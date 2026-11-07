// Native test of firmware/optional/locked_in_patch.h: the real header, compiled for the Mac
// against stubs of the Arduino APIs it calls, with a fake millis(). It checks the patch's
// logic (400 ms expiry, latch until Stop, telemetry), not the hardware: timing on the
// ATmega328P, the motor driver and the ESP32 relay are only proven on the robot (R5).
//
//   c++ -std=c++17 -Wall -Wextra -Werror -I firmware/test/stubs firmware/test/patch_harness.cpp -o patch_harness && ./patch_harness
//
// backend/tests/test_firmware_patch.py builds and runs it with the test suite.

#include <cstdio>
#include <cstdlib>

#include <Arduino.h>

// What Zeus_Car.ino gets from cmd_code_config.hpp before it includes the patch.
#define MODE_NONE 0
#define MODE_LINE_TRACK 1
#define MODE_APP_CONTROL 7
uint8_t currentMode = MODE_NONE;
static int stopCalls = 0;
void stop() {  // the sketch's stop(): MODE_NONE, remote values zeroed, carStop()
  currentMode = MODE_NONE;
  stopCalls++;
}

#include "ai_camera.h"
AiCamera aiCam;
int freeRam() { return 487; }

#include "../optional/locked_in_patch.h"

// ---------------------------------------------------------------- fakes
static uint32_t fakeNow = 0;
static unsigned long echoUs = 0;  // what pulseInLong() returns (0 = no echo)
static int pings = 0;
uint32_t millis() { return fakeNow; }
void pinMode(uint8_t, uint8_t) {}
void digitalWrite(uint8_t, uint8_t) {}
void delayMicroseconds(unsigned int) {}
unsigned long pulseInLong(uint8_t, uint8_t, unsigned long timeout) {
  pings++;
  return echoUs > timeout ? 0 : echoUs;
}
byte irObstacleRead() { return 0b11; }

static int failures = 0;
#define CHECK(cond)                                                    \
  do {                                                                 \
    if (!(cond)) {                                                     \
      std::printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond);      \
      failures++;                                                      \
    }                                                                  \
  } while (0)

static void reset(uint32_t now = 1000) {
  fakeNow = now;
  currentMode = MODE_NONE;
  stopCalls = 0;
  pings = 0;
  echoUs = 0;
  lockedInLastDataMs = now;
  lockedInWatchdogTrips = 0;
  lockedInLastTelemetryMs = 0;
  lockedInLatched = false;
  aiCam = AiCamera();
}

// A received WebSocket line: the patch's hook first, then (if not ignored) the stock handler,
// which for a joystick frame puts the car in app control and for F stops it.
static bool receive(bool stopButton) {
  aiCam.buttons[REGION_F] = stopButton;
  if (lockedInOnReceive()) return false;  // ignored
  if (stopButton) stop();
  else currentMode = MODE_APP_CONTROL;
  return true;
}

static void run(uint32_t ms, uint32_t frameEveryMs = 0) {
  for (uint32_t t = 0; t < ms; t++) {
    fakeNow++;
    if (frameEveryMs && fakeNow % frameEveryMs == 0) receive(false);
    lockedInLoop();
  }
}

static void test_frames_at_10_hz_never_trip() {
  reset();
  receive(false);
  run(5000, 100);
  CHECK(currentMode == MODE_APP_CONTROL);
  CHECK(lockedInWatchdogTrips == 0 && stopCalls == 0 && !lockedInLatched);
}

static void test_silence_stops_the_car_after_400_ms_not_before() {
  reset();
  receive(false);
  run(400);  // exactly 400 ms of silence: still driving (the check is "more than")
  CHECK(currentMode == MODE_APP_CONTROL && stopCalls == 0);
  run(1);  // 401 ms
  CHECK(currentMode == MODE_NONE && stopCalls == 1);
  CHECK(lockedInWatchdogTrips == 1 && lockedInLatched);
  run(3000);  // stays stopped; one trip per stall
  CHECK(stopCalls == 1 && lockedInWatchdogTrips == 1);
}

static void test_stale_frames_after_a_trip_cannot_restart_the_wheels() {
  reset();
  receive(false);
  run(500);
  CHECK(lockedInLatched);
  CHECK(!receive(false));  // a delayed joystick frame arrives after the stall
  CHECK(currentMode == MODE_NONE);
  CHECK(!receive(false));
  CHECK(currentMode == MODE_NONE && lockedInLatched);
  CHECK(receive(true));  // Stop clears the latch and runs the normal stop path
  CHECK(!lockedInLatched && currentMode == MODE_NONE && stopCalls == 2);
  CHECK(receive(false));  // driving works again after Stop
  CHECK(currentMode == MODE_APP_CONTROL);
}

static void test_other_modes_are_never_stopped_by_the_watchdog() {
  reset();
  currentMode = MODE_LINE_TRACK;  // stock line tracking runs without the app
  run(5000);
  CHECK(currentMode == MODE_LINE_TRACK && stopCalls == 0 && lockedInWatchdogTrips == 0);
}

static void test_millis_wraparound_is_not_a_stall() {
  reset(0xFFFFFF00u);
  receive(false);
  run(0x200, 100);  // crosses 2^32 while frames keep coming
  CHECK(fakeNow < 0x200);
  CHECK(currentMode == MODE_APP_CONTROL && stopCalls == 0);
}

static void test_telemetry_reports_version_latch_and_distance() {
  reset();
  run(200);
  CHECK(aiCam.send_doc.empty());  // no client: nothing sent, no ultrasonic pings
  CHECK(pings == 0);
  aiCam.ws_connected = true;
  echoUs = 1000;  // 17 cm
  run(LOCKED_IN_TELEMETRY_MS);
  CHECK(aiCam.send_doc["li"] == 2);
  CHECK(aiCam.send_doc["us"] > 16.9 && aiCam.send_doc["us"] < 17.1);
  CHECK(aiCam.send_doc["lk"] == 0 && aiCam.send_doc["wd"] == 0 && aiCam.send_doc["md"] == MODE_NONE);
  CHECK(aiCam.send_doc["ir"] == 3 && aiCam.send_doc["fr"] == 487);
  echoUs = 0;
  run(LOCKED_IN_TELEMETRY_MS);
  CHECK(aiCam.send_doc["us"] == -1);  // no echo within ~2 m
  receive(false);
  run(500);  // trip
  CHECK(aiCam.send_doc["lk"] == 1 && aiCam.send_doc["wd"] == 1);
  currentMode = MODE_LINE_TRACK;
  int before = pings;
  run(LOCKED_IN_TELEMETRY_MS);
  CHECK(aiCam.send_doc["us"] == -2 && pings == before);  // never pings in stock modes
}

int main() {
  test_frames_at_10_hz_never_trip();
  test_silence_stops_the_car_after_400_ms_not_before();
  test_stale_frames_after_a_trip_cannot_restart_the_wheels();
  test_other_modes_are_never_stopped_by_the_watchdog();
  test_millis_wraparound_is_not_a_stall();
  test_telemetry_reports_version_latch_and_distance();
  if (failures) {
    std::printf("%d check(s) failed\n", failures);
    return 1;
  }
  std::printf("ALL PASSED (locked_in_patch.h v%d, timeout %d ms)\n", LOCKED_IN_PATCH_VERSION, LOCKED_IN_MOTION_TIMEOUT_MS);
  return 0;
}
