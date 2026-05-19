/*
 * ESPectre - Sensor Publisher Implementation
 * 
 * Author: Francesco Pace <francesco.pace@gmail.com>
 * License: GPLv3
 */

#include "sensor_publisher.h"
#include "utils.h"
#include "esphome/core/log.h"
#include "esp_timer.h"
#include "esp_wifi.h"

namespace esphome {
namespace espectre {

void SensorPublisher::publish_all(const BaseDetector *detector,
                                  MotionState motion_state) {
  if (!detector) {
    return;
  }
  
  // Get current values
  float motion_metric = detector->get_motion_metric();
  bool is_motion = (motion_state == MotionState::MOTION);
  
  // Publish motion sensors
  if (motion_binary_sensor_) {
    motion_binary_sensor_->publish_state(is_motion);
  }

  if (movement_sensor_) {
    movement_sensor_->publish_state(motion_metric);
  }

  // Extended sensors (fork additions)
  if (breathing_sensor_) {
    breathing_sensor_->publish_state(detector->get_breathing_score());
  }

  if (phase_turbulence_sensor_) {
    phase_turbulence_sensor_->publish_state(detector->get_last_phase_turbulence());
  }

  if (dser_sensor_) {
    dser_sensor_->publish_state(detector->get_last_dser());
  }

  if (plcr_sensor_) {
    plcr_sensor_->publish_state(detector->get_last_plcr());
  }

  if (breathing_rate_sensor_) {
    float bpm = detector->get_breathing_rate_bpm();
    // Publish NaN when no signal (0 BPM = no estimate yet)
    breathing_rate_sensor_->publish_state(bpm > 0.0f ? bpm : NAN);
  }

  if (presence_binary_sensor_) {
    // Presence = motion OR (breathing elevated AND phase elevated)
    // This detects stationary occupants who don't trigger motion variance
    bool is_presence = is_motion;
    if (!is_motion && detector->is_ready() && detector->are_idle_baselines_initialized()) {
      float breath = detector->get_breathing_score();
      float phase = detector->get_last_phase_turbulence();
      float idle_phase = detector->get_idle_mean_phase_turbulence();
      float idle_breath = detector->get_idle_amplitude_baseline() * 0.01f;
      if (idle_breath < 0.001f) idle_breath = 0.001f;
      bool breathing_elevated = breath > idle_breath * 2.0f;
      bool phase_elevated = idle_phase > 0.001f && phase > idle_phase * 1.5f;
      is_presence = breathing_elevated && phase_elevated;
    }
    presence_binary_sensor_->publish_state(is_presence);
  }
}

void SensorPublisher::log_status(const char *tag,
                                 const BaseDetector *detector,
                                 MotionState motion_state,
                                 uint32_t packets_per_publish) {
  if (!detector || !tag) {
    return;
  }
  
  // Get current values
  float motion_metric = detector->get_motion_metric();
  float threshold = detector->get_threshold();
  bool is_motion = (motion_state == MotionState::MOTION);
  
  // Calculate CSI rate (packets per second)
  uint32_t now_ms = esp_timer_get_time() / 1000;
  uint32_t rate_pps = 0;
  if (last_log_time_ms_ > 0) {
    uint32_t elapsed_ms = now_ms - last_log_time_ms_;
    if (elapsed_ms > 0) {
      rate_pps = (packets_per_publish * 1000) / elapsed_ms;
    }
  }
  last_log_time_ms_ = now_ms;
  
  // Get WiFi info for diagnostics
  wifi_ap_record_t ap_info;
  int8_t rssi = -127;
  uint8_t channel = 0;
  if (esp_wifi_sta_get_ap_info(&ap_info) == ESP_OK) {
    rssi = ap_info.rssi;
    channel = ap_info.primary;
  }
  
  // Calculate progress
  float progress = (threshold > 0) ? (motion_metric / threshold) : 0.0f;
  int percent = (int)(progress * 100);

  // Log with progress bar, rate, and WiFi diagnostics
  log_progress_bar(tag, progress, 20, 15,
                   "%d%% | mvmt:%.4f thr:%.4f | %s | %u pkt/s | ch:%d rssi:%d",
                   percent, motion_metric, threshold,
                   is_motion ? "MOTION" : "IDLE",
                   rate_pps, channel, rssi);

  // RF health check — warn if RSSI outside recommended -40..-70 dB band.
  // Throttled to 1× per 60s to avoid log spam.
  if (rssi != -127 && (now_ms - last_rf_warn_ms_) > 60000) {
    if (rssi > -40) {
      ESP_LOGW(tag, "RSSI=%d dB too strong (>-40) — near AP, may saturate CSI", rssi);
      last_rf_warn_ms_ = now_ms;
    } else if (rssi < -70) {
      ESP_LOGW(tag, "RSSI=%d dB too weak (<-70) — low SNR, expect noisy CSI", rssi);
      last_rf_warn_ms_ = now_ms;
    }
  }
}

}  // namespace espectre
}  // namespace esphome
