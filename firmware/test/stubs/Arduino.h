// Native stand-in for the few Arduino APIs locked_in_patch.h uses (test harness only).
#pragma once
#include <cstdint>

typedef uint8_t byte;
#define OUTPUT 1
#define INPUT 0
#define LOW 0
#define HIGH 1

uint32_t millis();
void pinMode(uint8_t pin, uint8_t mode);
void digitalWrite(uint8_t pin, uint8_t value);
void delayMicroseconds(unsigned int us);
unsigned long pulseInLong(uint8_t pin, uint8_t state, unsigned long timeout);
