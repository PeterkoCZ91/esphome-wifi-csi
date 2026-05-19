/*
 * ESPectre - Sensor Publisher
 * 
 * Centralizes ESPHome sensor publishing logic.
 * Reduces code duplication and improves maintainability.
 * 
 * Author: Francesco Pace <francesco.pace@gmail.com>
 * License: GPLv3
 */

#pragma once

#include "esphome/components/sensor/sensor.h"
#include "esphome/components/binary_sensor/binary_sensor.h"
#include "utils.h"
#include "base_detector.h"

namespace esphome {
namespace espectre {

/**
 * Sensor Publisher
 * 
 * Manages publishing of all ESPectre sensors to ESPHome.
 * Handles both motion sensors and feature sensors.
 */
class SensorPublisher {
 public:
  // Motion sensors
  void set_movement_sensor(sensor::Sensor *sensor) { movement_sensor_ = sensor; }
  void set_motion_binary_sensor(binary_sensor::BinarySensor *sensor) { motion_binary_sensor_ = sensor; }

  // Extended sensors (fork additions)
  void set_breathing_sensor(sensor::Sensor *sensor) { breathing_sensor_ = sensor; }
  void set_phase_turbulence_sensor(sensor::Sensor *sensor) { phase_turbulence_sensor_ = sensor; }
  void set_presence_binary_sensor(binary_sensor::BinarySensor *sensor) { presence_binary_sensor_ = sensor; }
  void set_dser_sensor(sensor::Sensor *sensor) { dser_sensor_ = sensor; }
  void set_plcr_sensor(sensor::Sensor *sensor) { plcr_sensor_ = sensor; }
  void set_breathing_rate_sensor(sensor::Sensor *sensor) { breathing_rate_sensor_ = sensor; }
  
  /**
   * Publish all sensors with current values
   * 
   * @param detector Motion detector (BaseDetector*)
   * @param motion_state Current motion state
   */
  void publish_all(const BaseDetector *detector,
                   MotionState motion_state);
  
  /**
   * Log status with progress bar
   * 
   * @param tag Log tag
   * @param detector Motion detector
   * @param motion_state Current motion state
   * @param packets_per_publish Number of packets processed per publish cycle
   */
  void log_status(const char *tag,
                  const BaseDetector *detector,
                  MotionState motion_state,
                  uint32_t packets_per_publish);
  
  /**
   * Check if sensors are configured
   */
  bool has_movement_sensor() const { return movement_sensor_ != nullptr; }
  bool has_motion_binary_sensor() const { return motion_binary_sensor_ != nullptr; }
  bool has_breathing_sensor() const { return breathing_sensor_ != nullptr; }
  bool has_phase_turbulence_sensor() const { return phase_turbulence_sensor_ != nullptr; }
  bool has_presence_binary_sensor() const { return presence_binary_sensor_ != nullptr; }
  bool has_dser_sensor() const { return dser_sensor_ != nullptr; }
  bool has_plcr_sensor() const { return plcr_sensor_ != nullptr; }
  bool has_breathing_rate_sensor() const { return breathing_rate_sensor_ != nullptr; }
  
  /**
   * Reset rate counter
   */
  void reset_rate_counter() { last_log_time_ms_ = 0; }
  
 private:
  sensor::Sensor *movement_sensor_{nullptr};
  binary_sensor::BinarySensor *motion_binary_sensor_{nullptr};
  sensor::Sensor *breathing_sensor_{nullptr};
  sensor::Sensor *phase_turbulence_sensor_{nullptr};
  binary_sensor::BinarySensor *presence_binary_sensor_{nullptr};
  sensor::Sensor *dser_sensor_{nullptr};
  sensor::Sensor *plcr_sensor_{nullptr};
  sensor::Sensor *breathing_rate_sensor_{nullptr};
  uint32_t last_log_time_ms_{0};
  uint32_t last_rf_warn_ms_{0};
};

}  // namespace espectre
}  // namespace esphome
