/*
 * ESPectre - MVS Detector Implementation
 *
 * Moving Variance Segmentation (MVS) motion detection algorithm.
 *
 * Author: Francesco Pace <francesco.pace@gmail.com>
 * License: GPLv3
 */

#include "mvs_detector.h"
#include "utils.h"
#include <cmath>
#include <utility>
#include "esphome/core/log.h"

namespace esphome {
namespace espectre {

static const char *TAG = "MVSDetector";

// ============================================================================
// CONSTRUCTOR
// ============================================================================

MVSDetector::MVSDetector(uint16_t window_size, float threshold, float hysteresis)
    : BaseDetector(window_size)
    , threshold_(threshold)
    , hysteresis_factor_(hysteresis)
    , current_moving_variance_(0.0f) {
    threshold_ = clamp_threshold(threshold_, MVS_MIN_THRESHOLD, MVS_MAX_THRESHOLD);

    // Validate and clamp hysteresis
    if (hysteresis_factor_ < MVS_MIN_HYSTERESIS) {
        hysteresis_factor_ = MVS_MIN_HYSTERESIS;
    } else if (hysteresis_factor_ > MVS_MAX_HYSTERESIS) {
        hysteresis_factor_ = MVS_MAX_HYSTERESIS;
    }

    ESP_LOGI(TAG, "Initialized (window=%d, threshold=%.2f, hysteresis=%.2f)",
             window_size_, threshold_, hysteresis_factor_);
}

MVSDetector::MVSDetector(MVSDetector&& other) noexcept
    : BaseDetector(std::move(other))
    , threshold_(other.threshold_)
    , hysteresis_factor_(other.hysteresis_factor_)
    , current_moving_variance_(other.current_moving_variance_) {
}

MVSDetector& MVSDetector::operator=(MVSDetector&& other) noexcept {
    if (this != &other) {
        BaseDetector::operator=(std::move(other));
        threshold_ = other.threshold_;
        hysteresis_factor_ = other.hysteresis_factor_;
        current_moving_variance_ = other.current_moving_variance_;
    }
    return *this;
}

// ============================================================================
// DETECTION LOGIC
// ============================================================================

void MVSDetector::update_state() {
    // Calculate moving variance (lazy evaluation)
    current_moving_variance_ = calculate_moving_variance();

    // Raw decision based on variance vs threshold (with hysteresis)
    bool raw_motion;
    if (state_ == MotionState::IDLE) {
        raw_motion = current_moving_variance_ > threshold_;
    } else {
        raw_motion = current_moving_variance_ >= threshold_ * hysteresis_factor_;
    }

    // Apply temporal smoothing (N/M voting to reduce false toggles)
    state_ = apply_temporal_smoothing(raw_motion);

    // Update idle-gated baselines after state transition
    float turb = get_last_turbulence();
    float pt = get_last_phase_turbulence();
    const float* amps = get_last_amplitudes();
    uint8_t n = get_num_amplitudes();
    float amp_sum = 0;
    for (uint8_t i = 0; i < n; i++) amp_sum += amps[i];
    update_idle_baselines(turb, pt, amp_sum);
}

bool MVSDetector::set_threshold(float threshold) {
    if (!is_valid_threshold(threshold, MVS_MIN_THRESHOLD, MVS_MAX_THRESHOLD)) {
        ESP_LOGE(TAG, "Invalid threshold: %.2f (must be %.1f-%.1f)",
                 threshold, MVS_MIN_THRESHOLD, MVS_MAX_THRESHOLD);
        return false;
    }

    threshold_ = threshold;
    ESP_LOGD(TAG, "Threshold updated: %.2f", threshold);
    return true;
}

bool MVSDetector::set_hysteresis(float factor) {
    if (std::isnan(factor) || std::isinf(factor) ||
        factor < MVS_MIN_HYSTERESIS || factor > MVS_MAX_HYSTERESIS) {
        ESP_LOGE(TAG, "Invalid hysteresis: %.2f (must be %.1f-%.1f)",
                 factor, MVS_MIN_HYSTERESIS, MVS_MAX_HYSTERESIS);
        return false;
    }

    hysteresis_factor_ = factor;
    ESP_LOGI(TAG, "Hysteresis updated: %.2f", factor);
    return true;
}

// ============================================================================
// PRIVATE METHODS
// ============================================================================

float MVSDetector::calculate_moving_variance() const {
    if (buffer_count_ < window_size_) {
        return 0.0f;
    }

    return calculate_variance_two_pass(turbulence_buffer_, window_size_);
}

}  // namespace espectre
}  // namespace esphome
