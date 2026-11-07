/*
 * LOCKED IN patch for the SunFounder Zeus Car sketch (Zeus_Car.ino 1.4.5 and 1.4.6).
 * Licensed GPL-3.0-or-later, like the upstream sketch it extends.
 *
 * 1. Device-side motion watchdog. While the car is in MODE_APP_CONTROL, if no
 *    WebSocket data has reached the Arduino for LOCKED_IN_MOTION_TIMEOUT_MS, call the
 *    sketch's own stop() (the same path as the app's Stop button: MODE_NONE, remote
 *    values zeroed, carStop()). Stock firmware only stops when the ESP32 reports the
 *    client gone or after its 3-4 s data timeout.
 *    After a trip the watchdog latches: received lines are ignored until one carries
 *    the Stop button (F), so stale motion frames delivered after a Wi-Fi stall cannot
 *    restart the wheels. LOCKED IN sends Stop after every motion and every 500 ms when
 *    idle, which clears the latch.
 * 2. Telemetry. While a WebSocket client is connected, adds ultrasonic distance, IR
 *    obstacle bits, mode, watchdog trip count and free RAM to the JSON the sketch
 *    already sends back after received lines. The ultrasonic read is bounded to
 *    ~12 ms (the stock ultrasonicRead() uses pulseIn() with its 1 s default timeout)
 *    and only runs in MODE_NONE / MODE_APP_CONTROL so it never adds pings or delay
 *    to the stock line-tracking / obstacle modes.
 *
 * Side effects: a controller that stops sending while the joystick is held steady
 * will see the car stop after the timeout, and must press Stop before driving again.
 * LOCKED IN refreshes motion frames at 10 Hz, so it never trips during intended
 * motion unless the link stalls.
 *
 * Reported keys (all optional for the host):
 *   li  patch version
 *   us  ultrasonic cm; -1 = no echo within ~2 m; -2 = not measured in this mode
 *   ir  IR obstacle bits (bit0 left clear, bit1 right clear, as in the sketch)
 *   md  currentMode
 *   wd  watchdog trips since boot
 *   lk  1 while latched after a trip (waiting for Stop)
 *   fr  free RAM between heap and stack at the telemetry call, bytes
 */
#ifndef __LOCKED_IN_PATCH_H__
#define __LOCKED_IN_PATCH_H__

#include <Arduino.h>
#include "ai_camera.h"
#include "ir_obstacle.h"
#include "ultrasonic.h"

#define LOCKED_IN_PATCH_VERSION 2
#define LOCKED_IN_MOTION_TIMEOUT_MS 400
#define LOCKED_IN_TELEMETRY_MS 120
#define LOCKED_IN_US_TIMEOUT_US 12000UL // ~2 m round trip

extern AiCamera aiCam;
int freeRam(); // defined in Zeus_Car.ino

static uint32_t lockedInLastDataMs = 0;
static uint16_t lockedInWatchdogTrips = 0;
static uint32_t lockedInLastTelemetryMs = 0;
static bool lockedInLatched = false;

static float lockedInUltrasonicCm()
{
  pinMode(ULTRASONIC_TRIG_PIN, OUTPUT);
  digitalWrite(ULTRASONIC_TRIG_PIN, LOW);
  delayMicroseconds(2);
  digitalWrite(ULTRASONIC_TRIG_PIN, HIGH);
  delayMicroseconds(10);
  digitalWrite(ULTRASONIC_TRIG_PIN, LOW);
  pinMode(ULTRASONIC_ECHO_PIN, INPUT);
  // pulseInLong() times with micros(), so SoftPWM's timer interrupt (motor PWM) does not
  // shorten the measurement the way it does pulseIn()'s cycle-counting loop.
  unsigned long duration = pulseInLong(ULTRASONIC_ECHO_PIN, HIGH, LOCKED_IN_US_TIMEOUT_US);
  if (duration == 0)
  {
    return -1; // no echo in range
  }
  return duration * 0.017;
}

// Call at the top of onReceive(): a line of WebSocket data reached the Arduino.
// Returns true when onReceive() must ignore the line (latched after a trip, not a Stop).
static inline bool lockedInOnReceive()
{
  lockedInLastDataMs = millis();
  if (!lockedInLatched)
  {
    return false;
  }
  if (aiCam.getButton(REGION_F))
  {
    lockedInLatched = false;
    return false; // onReceive() then runs the normal Stop path
  }
  return true;
}

// Call from loop() right after aiCam.loop().
static void lockedInLoop()
{
  uint32_t now = millis();
  if (currentMode == MODE_APP_CONTROL && now - lockedInLastDataMs > LOCKED_IN_MOTION_TIMEOUT_MS)
  {
    stop();
    lockedInWatchdogTrips++;
    lockedInLatched = true;
  }
  if (aiCam.ws_connected && now - lockedInLastTelemetryMs >= LOCKED_IN_TELEMETRY_MS)
  {
    lockedInLastTelemetryMs = now;
    bool measure = currentMode == MODE_NONE || currentMode == MODE_APP_CONTROL;
    aiCam.send_doc["li"] = LOCKED_IN_PATCH_VERSION;
    aiCam.send_doc["us"] = measure ? lockedInUltrasonicCm() : -2;
    aiCam.send_doc["ir"] = irObstacleRead();
    aiCam.send_doc["md"] = currentMode;
    aiCam.send_doc["wd"] = lockedInWatchdogTrips;
    aiCam.send_doc["lk"] = lockedInLatched ? 1 : 0;
    aiCam.send_doc["fr"] = freeRam();
  }
}

#endif // __LOCKED_IN_PATCH_H__
