/*
 * ESPectre - CSI Manager
 * 
 * Manages ESP32 CSI (Channel State Information) hardware configuration.
 * Handles platform-specific differences (ESP32-C6 vs ESP32-S3).
 * 
 * Author: Francesco Pace <francesco.pace@gmail.com>
 * License: GPLv3
 */

#pragma once

#include "esp_wifi.h"
#include "esp_err.h"
#include "esp_attr.h"
#include "utils.h"
#include "base_detector.h"
#include "wifi_csi_interface.h"
#include "gain_controller.h"
#include <functional>

namespace esphome {
namespace espectre {

// Forward declaration
class NBVICalibrator;

// Callback type for processed CSI data
using csi_processed_callback_t = std::function<void(MotionState, uint32_t)>;

// Callback type for game mode (called every packet with movement and threshold)
using game_mode_callback_t = std::function<void(float movement, float threshold)>;

/**
 * CSI Manager
 * 
 * Manages complete CSI pipeline: hardware configuration, data processing, and motion detection.
 * Handles platform-specific differences between ESP32-C6 and ESP32-S3.
 * Orchestrates CSI packet processing and band calibration.
 */
class CSIManager {
 public:
  /**
   * Initialize CSI Manager
   * 
   * @param detector Motion detector instance (BaseDetector*)
   * @param selected_subcarriers Initial subcarrier selection (array of 12 subcarriers)
   * @param publish_rate Number of packets before triggering callback
   * @param gain_lock_mode Gain lock mode (auto/enabled/disabled)
   * @param wifi_csi WiFi CSI interface (nullptr for real implementation)
   */
  void init(BaseDetector* detector,
            const uint8_t selected_subcarriers[12],
            uint32_t publish_rate,
            GainLockMode gain_lock_mode = GainLockMode::AUTO,
            IWiFiCSI* wifi_csi = nullptr);
  
  /**
   * Update subcarrier selection
   * 
   * @param subcarriers New subcarrier selection (array of 12 subcarriers)
   */
  void update_subcarrier_selection(const uint8_t subcarriers[12]);
  
  /**
   * Update segmentation threshold
   * 
   * @param threshold New threshold value
   */
  void set_threshold(float threshold);
  
  /**
   * Enable CSI hardware and start processing
   * 
   * @param packet_callback Callback to invoke periodically (every publish_rate packets)
   * @return ESP_OK on success
   */
  esp_err_t enable(csi_processed_callback_t packet_callback = nullptr);
  
  /**
   * Disable CSI hardware
   * 
   * @return ESP_OK on success
   */
  esp_err_t disable();
  
  /**
   * Process incoming CSI packet
   * 
   * Orchestrates: calibration check → processing → callbacks
   * 
   * @param data CSI packet data
   */
  void process_packet(wifi_csi_info_t* data);
  
  /**
   * Set calibration mode
   * 
   * When a calibrator is set, CSI packets are routed to it during calibration.
   * 
   * @param calibrator Calibrator instance (nullptr to disable calibration mode)
   */
  void set_calibration_mode(NBVICalibrator* calibrator) { calibrator_ = calibrator; }
  
  /**
   * Check if CSI is currently enabled
   */
  bool is_enabled() const { return enabled_; }
  
  /**
   * Check if gain is locked
   */
  bool is_gain_locked() const { return gain_controller_.is_locked(); }
  
  /**
   * Get the number of packets used for gain lock calibration
   */
  uint16_t get_gain_lock_packets() const { return gain_controller_.get_calibration_packets(); }
  
  /**
   * Get the gain controller (for status reporting)
   */
  const GainController& get_gain_controller() const { return gain_controller_; }
  
  /**
   * Set callback for when gain lock completes
   */
  void set_gain_lock_callback(GainController::lock_complete_callback_t callback) {
    gain_controller_.set_lock_complete_callback(callback);
  }
  
  /**
   * Set game mode callback
   */
  void set_game_mode_callback(game_mode_callback_t callback) {
    game_mode_callback_ = callback;
  }
  
  /**
   * Get the detector instance
   */
  BaseDetector* get_detector() { return detector_; }

  /**
   * Runtime CSI diagnostics. These are intentionally simple getters so
   * YAML template sensors can expose chip/AP-specific CSI behavior.
   */
  uint32_t get_raw_packets_total() const { return raw_packets_total_; }
  uint32_t get_valid_packets_total() const { return packets_total_; }
  uint32_t get_filtered_packets_total() const { return packets_filtered_; }
  uint32_t get_raw_packet_rate_pps() const { return raw_packet_rate_pps_; }
  uint16_t get_last_raw_csi_len() const { return last_raw_csi_len_; }
  uint16_t get_last_valid_csi_len() const { return last_valid_csi_len_; }
  int8_t get_last_packet_rssi() const { return last_packet_rssi_; }
  uint8_t get_last_packet_channel() const { return last_packet_channel_; }
  uint8_t get_last_signal_mode() const { return last_signal_mode_; }
  uint8_t get_last_channel_bandwidth() const { return last_channel_bandwidth_; }
  uint8_t get_last_mcs() const { return last_mcs_; }
  uint8_t get_last_bb_format() const { return last_bb_format_; }
  uint16_t get_last_channel_estimate_len() const { return last_channel_estimate_len_; }
  bool get_last_channel_estimate_valid() const { return last_channel_estimate_valid_; }
  
  /**
   * Clear detector buffer (for calibration reset)
   */
  void clear_detector_buffer();

  /**
   * Peer MAC filter for pairwise/mesh sensing.
   * When set, only CSI packets from this MAC are processed.
   * Promiscuous mode is enabled automatically so off-AP frames are captured.
   */
  void set_peer_mac_filter(const uint8_t mac[6]);
  void clear_peer_mac_filter();
  bool has_peer_mac_filter() const { return peer_mac_filter_set_; }
  void add_extra_peer_mac(const uint8_t mac[6]);
  void clear_extra_peer_macs();
  /**
   * Accept broadcast MAC alongside peer_mac_filter_.
   *
   * ESP-NOW Action frames on ESP32-C5 may report FF:FF:FF:FF:FF:FF in
   * wifi_csi_info_t.mac instead of the actual sender MAC.
   */
  void set_peer_mac_filter_also_broadcast(bool accept_broadcast) {
    peer_mac_accept_broadcast_ = accept_broadcast;
  }
  /**
   * Accept the currently associated AP BSSID alongside peer_mac_filter_.
   *
   * ESP32-C5 5GHz CSI callbacks can report the AP BSSID for observed frames.
   */
  void set_peer_mac_filter_also_ap_bssid(bool accept_ap_bssid) {
    peer_mac_accept_ap_bssid_ = accept_ap_bssid;
    peer_mac_ap_bssid_cached_ = false;
  }

 private:
  static void IRAM_ATTR csi_rx_callback_wrapper_(void* ctx, wifi_csi_info_t* data);
  
  bool enabled_{false};
  BaseDetector* detector_{nullptr};
  const uint8_t* selected_subcarriers_{nullptr};
  NBVICalibrator* calibrator_{nullptr};
  csi_processed_callback_t packet_callback_;
  game_mode_callback_t game_mode_callback_;
  uint32_t publish_rate_{100};
  volatile uint32_t packets_processed_{0};
  volatile uint32_t packets_filtered_{0};
  uint32_t packets_total_{0};
  uint32_t raw_packets_total_{0};
  uint8_t current_channel_{0};

  uint32_t raw_packet_rate_pps_{0};
  uint32_t raw_rate_window_count_{0};
  uint32_t raw_rate_window_start_ms_{0};
  uint16_t last_raw_csi_len_{0};
  uint16_t last_valid_csi_len_{0};
  int8_t last_packet_rssi_{-127};
  uint8_t last_packet_channel_{0};
  uint8_t last_signal_mode_{0};
  uint8_t last_channel_bandwidth_{0};
  uint8_t last_mcs_{0};
  uint8_t last_bb_format_{0};
  uint16_t last_channel_estimate_len_{0};
  bool last_channel_estimate_valid_{false};
  
  uint8_t peer_mac_filter_[6]{};
  bool peer_mac_filter_set_{false};
  bool peer_mac_accept_broadcast_{false};
  bool peer_mac_accept_ap_bssid_{false};
  uint8_t peer_mac_ap_bssid_[6]{};
  bool peer_mac_ap_bssid_cached_{false};

  // Multi-peer MAC support (mesh TX-from-multiple-nodes)
  static constexpr uint8_t MAX_EXTRA_PEER_MACS = 4;
  uint8_t extra_peer_macs_[MAX_EXTRA_PEER_MACS][6]{};
  uint8_t extra_peer_mac_count_{0};

  // Diagnostic: track unique MACs seen in CSI callback (debug builds only).
  uint8_t seen_macs_[10][6]{};
  uint8_t unique_macs_logged_{0};

  IWiFiCSI* wifi_csi_{nullptr};
  WiFiCSIReal default_wifi_csi_;
  GainController gain_controller_;
  
  // Active subcarrier map — switches per-packet between HT20 and HT40 maps
  const uint8_t* active_subcarrier_map_{DEFAULT_SUBCARRIERS};
  uint8_t active_num_subcarriers_{HT20_SELECTED_BAND_SIZE};
  uint32_t ht40_packets_{0};
  uint32_t ht20_packets_{0};
  
  esp_err_t configure_platform_specific_();
};

}  // namespace espectre
}  // namespace esphome
