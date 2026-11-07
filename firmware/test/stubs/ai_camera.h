// Native stand-in for the SunFounder AiCamera class: only what locked_in_patch.h touches.
#pragma once
#include <cstdint>
#include <map>
#include <string>

#define REGION_F 5 // the Stop button, as in ai_camera.h

class AiCamera {
 public:
  bool ws_connected = false;
  std::map<std::string, double> send_doc; // StaticJsonDocument<200> on the robot
  bool buttons[26] = {};                   // regions of the last received line
  bool getButton(uint8_t region) { return buttons[region]; }
};
