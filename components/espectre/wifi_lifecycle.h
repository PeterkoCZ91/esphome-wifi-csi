/*
 * ESPectre - WiFi Lifecycle Manager
 * 
 * Manages WiFi connection lifecycle and coordinates service startup/shutdown.
 * Handles CSI, Traffic Generator, and Band Calibration orchestration.
 * 
 * Author: Francesco Pace <francesco.pace@gmail.com>
 * License: GPLv3
 */

#pragma once

#include "esp_event.h"
#include "esp_err.h"
#include <functional>

namespace esphome {
namespace espectre {

// Callback types
using wifi_connected_callback_t = std::function<void()>;
using wifi_disconnected_callback_t = std::function<void()>;

// WiFi band used for CSI capture (only honored on dual-band ESP32-C5)
enum class WiFiBandMode {
  BAND_2G,  // 2.4 GHz only (default)
  BAND_5G,  // 5 GHz only
  AUTO      // 2.4 + 5 GHz, driver/AP decides
};

/**
 * WiFi Lifecycle Manager
 * 
 * Manages WiFi connection events and coordinates service lifecycle.
 * Handles startup sequence: CSI → Traffic Generator → Band Calibration
 */
class WiFiLifecycleManager {
 public:
  /**
   * Initialize WiFi for optimal CSI capture
   * 
   * Configures WiFi settings critical for CSI:
   * - Promiscuous mode
   * - Power save disabled
   * - Protocol (b/g/n or b/g/n/ax for ESP32-C6)
   * - Bandwidth HT20
   * 
   * @return ESP_OK on success
   */
  esp_err_t init();

  /**
   * Set WiFi band for CSI capture (applied in init(), ESP32-C5 only)
   */
  void set_band_mode(WiFiBandMode mode) { band_mode_ = mode; }
  WiFiBandMode get_band_mode() const { return band_mode_; }
  const char *get_band_mode_str() const;
  
  /**
   * Register WiFi event handlers
   * 
   * @param connected_cb Callback when WiFi connects
   * @param disconnected_cb Callback when WiFi disconnects
   * @return ESP_OK on success
   */
  esp_err_t register_handlers(wifi_connected_callback_t connected_cb,
                              wifi_disconnected_callback_t disconnected_cb);
  
  /**
   * Unregister WiFi event handlers
   */
  void unregister_handlers();
  
 private:
  // Static handlers for ESP-IDF C API (separated by event type)
  static void ip_event_handler_(void* arg, esp_event_base_t event_base,
                                int32_t event_id, void* event_data);
  static void wifi_event_handler_(void* arg, esp_event_base_t event_base,
                                  int32_t event_id, void* event_data);
  
  // Callbacks
  wifi_connected_callback_t connected_callback_;
  wifi_disconnected_callback_t disconnected_callback_;
  
  WiFiBandMode band_mode_{WiFiBandMode::BAND_2G};

  // Event handler instances
  esp_event_handler_instance_t connected_instance_{nullptr};
  esp_event_handler_instance_t disconnected_instance_{nullptr};
};

}  // namespace espectre
}  // namespace esphome
