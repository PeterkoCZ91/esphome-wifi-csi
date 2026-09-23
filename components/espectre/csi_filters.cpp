/*
 * ESPectre - CSI Filters Implementation
 *
 * Low-pass and Hampel filter implementations for signal processing.
 *
 * Author: Francesco Pace <francesco.pace@gmail.com>
 * License: GPLv3
 */

#include "filters.h"
#include "utils.h"
#include <cmath>
#include <cstring>
#include <cstdlib>
#include "esphome/core/log.h"

namespace esphome {
namespace espectre {

static const char *TAG = "CSI_Filters";

// ============================================================================
// LOW-PASS FILTER IMPLEMENTATION
// ============================================================================

void lowpass_filter_init(lowpass_filter_state_t *state, float cutoff_hz, float sample_rate_hz, bool enabled) {
    if (!state) {
        ESP_LOGE(TAG, "lowpass_filter_init: NULL state pointer");
        return;
    }

    // Clamp cutoff to valid range
    if (cutoff_hz < LOWPASS_CUTOFF_MIN) cutoff_hz = LOWPASS_CUTOFF_MIN;
    if (cutoff_hz > LOWPASS_CUTOFF_MAX) cutoff_hz = LOWPASS_CUTOFF_MAX;

    state->cutoff_hz = cutoff_hz;
    state->enabled = enabled;
    state->initialized = false;
    state->x_prev = 0.0f;
    state->y_prev = 0.0f;

    // Calculate filter coefficients using bilinear transform
    float wc = tanf(M_PI * cutoff_hz / sample_rate_hz);
    float k = 1.0f + wc;

    state->b0 = wc / k;
    state->a1 = (wc - 1.0f) / k;

    ESP_LOGD(TAG, "LowPass filter initialized: cutoff=%.1f Hz, enabled=%d", cutoff_hz, enabled);
}

float lowpass_filter_apply(lowpass_filter_state_t *state, float value) {
    if (!state || !state->enabled) {
        return value;
    }

    if (!state->initialized) {
        state->x_prev = value;
        state->y_prev = value;
        state->initialized = true;
        return value;
    }

    float y = state->b0 * value + state->b0 * state->x_prev - state->a1 * state->y_prev;
    state->x_prev = value;
    state->y_prev = y;

    return y;
}

void lowpass_filter_reset(lowpass_filter_state_t *state) {
    if (!state) return;
    state->x_prev = 0.0f;
    state->y_prev = 0.0f;
    state->initialized = false;
}

// ============================================================================
// HAMPEL FILTER IMPLEMENTATION
// ============================================================================

void hampel_turbulence_init(hampel_turbulence_state_t *state, uint8_t window_size, float threshold, bool enabled) {
    if (!state) {
        ESP_LOGE(TAG, "hampel_turbulence_init: NULL state pointer");
        return;
    }

    if (window_size < HAMPEL_TURBULENCE_WINDOW_MIN || window_size > HAMPEL_TURBULENCE_WINDOW_MAX) {
        ESP_LOGW(TAG, "Invalid Hampel window size %d, using default %d",
                 window_size, HAMPEL_TURBULENCE_WINDOW_DEFAULT);
        window_size = HAMPEL_TURBULENCE_WINDOW_DEFAULT;
    }

    std::memset(state->buffer, 0, sizeof(state->buffer));
    std::memset(state->sorted_buffer, 0, sizeof(state->sorted_buffer));
    std::memset(state->deviations, 0, sizeof(state->deviations));
    state->window_size = window_size;
    state->index = 0;
    state->count = 0;
    state->threshold = threshold;
    state->enabled = enabled;
}

float hampel_filter(const float *window, size_t window_size,
                    float current_value, float threshold) {
    if (!window || window_size < 3) {
        return current_value;
    }

    // Stack allocation - window_size is bounded (3-11 max)
    float sorted[HAMPEL_TURBULENCE_WINDOW_MAX];
    float abs_deviations[HAMPEL_TURBULENCE_WINDOW_MAX];

    // Clamp to max supported window size
    if (window_size > HAMPEL_TURBULENCE_WINDOW_MAX) {
        window_size = HAMPEL_TURBULENCE_WINDOW_MAX;
    }

    std::memcpy(sorted, window, window_size * sizeof(float));
    float median = calculate_median_float(sorted, window_size);

    for (size_t i = 0; i < window_size; i++) {
        abs_deviations[i] = std::abs(window[i] - median);
    }
    float mad = calculate_median_float(abs_deviations, window_size);

    float mad_scaled = MAD_SCALE_FACTOR * mad;
    float deviation = std::abs(current_value - median);

    if (deviation > threshold * mad_scaled) {
        return median;
    }

    return current_value;
}

float hampel_filter_turbulence(hampel_turbulence_state_t *state, float turbulence) {
    if (!state || !state->enabled) {
        return turbulence;
    }

    state->buffer[state->index] = turbulence;
    state->index = (state->index + 1) % state->window_size;
    if (state->count < state->window_size) {
        state->count++;
    }

    if (state->count < 3) {
        return turbulence;
    }

    size_t n = state->count;
    std::memcpy(state->sorted_buffer, state->buffer, n * sizeof(float));
    float median = calculate_median_float(state->sorted_buffer, n);

    for (size_t i = 0; i < n; i++) {
        state->deviations[i] = std::abs(state->buffer[i] - median);
    }
    float mad = calculate_median_float(state->deviations, n);

    float deviation = std::abs(turbulence - median);

    if (deviation > state->threshold * MAD_SCALE_FACTOR * mad) {
        return median;
    }

    return turbulence;
}

// ============================================================================
// BREATHING BANDPASS FILTER IMPLEMENTATION
// ============================================================================
// Cascaded 1st-order Butterworth HP (0.08 Hz) + LP (0.6 Hz) via bilinear transform
// with prewarping: wc = tan(pi * fc / fs)
//   HP: b0 = 1/(1+wc),  a1 = (wc-1)/(1+wc)
//   LP: b0 = wc/(1+wc), a1 = (wc-1)/(1+wc)
// At fs=100 Hz this gives HP b0=0.99749 a1=-0.99498, LP b0=0.01850 a1=-0.96300.
// Energy EMA alpha = 1/(tau*fs), tau = 3 s (alpha = 1/300 at 100 Hz).

void breathing_filter_set_sample_rate(breathing_filter_state_t *state, float sample_rate_hz) {
    if (!state) return;
    // Nyquist guard: LP cutoff must stay well below fs/2
    if (!(sample_rate_hz >= 2.0f)) sample_rate_hz = 2.0f;

    const float hp_wc = std::tan(static_cast<float>(M_PI) * BREATHING_HP_CUTOFF_HZ / sample_rate_hz);
    state->hp_b0 = 1.0f / (1.0f + hp_wc);
    state->hp_a1 = (hp_wc - 1.0f) / (1.0f + hp_wc);

    const float lp_wc = std::tan(static_cast<float>(M_PI) * BREATHING_LP_CUTOFF_HZ / sample_rate_hz);
    state->lp_b0 = lp_wc / (1.0f + lp_wc);
    state->lp_a1 = (lp_wc - 1.0f) / (1.0f + lp_wc);

    float alpha = 1.0f / (BREATHING_ENERGY_TAU_S * sample_rate_hz);
    state->energy_alpha = (alpha > 1.0f) ? 1.0f : alpha;
    state->sample_rate = sample_rate_hz;
}

void breathing_filter_init(breathing_filter_state_t *state) {
    if (!state) return;
    state->hp_x_prev = 0.0f;
    state->hp_y_prev = 0.0f;
    state->lp_x_prev = 0.0f;
    state->lp_y_prev = 0.0f;
    state->energy = 0.0f;
    state->initialized = false;
    breathing_filter_set_sample_rate(state, BREATHING_DEFAULT_SAMPLE_RATE);
}

float breathing_filter_apply(breathing_filter_state_t *state, float amplitude_sum) {
    if (!state) return 0.0f;

    if (!state->initialized) {
        state->hp_x_prev = amplitude_sum;
        state->hp_y_prev = 0.0f;
        state->lp_x_prev = 0.0f;
        state->lp_y_prev = 0.0f;
        state->energy = 0.0f;
        state->initialized = true;
        return 0.0f;
    }

    // High-pass: y = b0 * (x - x_prev) - a1 * y_prev
    float hp_out = state->hp_b0 * (amplitude_sum - state->hp_x_prev) - state->hp_a1 * state->hp_y_prev;
    state->hp_x_prev = amplitude_sum;
    state->hp_y_prev = hp_out;

    // Low-pass: y = b0 * (x + x_prev) - a1 * y_prev
    float lp_out = state->lp_b0 * (hp_out + state->lp_x_prev) - state->lp_a1 * state->lp_y_prev;
    state->lp_x_prev = hp_out;
    state->lp_y_prev = lp_out;

    // Energy estimation: EMA of squared signal
    float sq = lp_out * lp_out;
    state->energy = state->energy_alpha * sq + (1.0f - state->energy_alpha) * state->energy;

    return lp_out;
}

float breathing_filter_get_score(const breathing_filter_state_t *state) {
    if (!state) return 0.0f;
    return std::sqrt(state->energy);  // RMS of bandpassed signal
}

}  // namespace espectre
}  // namespace esphome
