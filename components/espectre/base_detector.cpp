/*
 * ESPectre - Base Detector Implementation
 *
 * Abstract base class for motion detection algorithms.
 *
 * Author: Francesco Pace <francesco.pace@gmail.com>
 * License: GPLv3
 */

#include "base_detector.h"
#include "utils.h"
#include <cmath>
#include <cstring>
#include <new>
#include "esp_timer.h"
#include "esphome/core/log.h"

namespace esphome {
namespace espectre {

static const char *TAG = "BaseDetector";

// ============================================================================
// CONSTRUCTOR / DESTRUCTOR
// ============================================================================

BaseDetector::BaseDetector(uint16_t window_size)
    : turbulence_buffer_(nullptr)
    , num_amplitudes_(0)
    , buffer_index_(0)
    , buffer_count_(0)
    , window_size_(window_size)
    , state_(MotionState::IDLE)
    , total_packets_(0)
    , packet_index_(0) {

    // Validate and clamp window size
    if (window_size_ < DETECTOR_MIN_WINDOW_SIZE) {
        window_size_ = DETECTOR_MIN_WINDOW_SIZE;
    } else if (window_size_ > DETECTOR_MAX_WINDOW_SIZE) {
        window_size_ = DETECTOR_MAX_WINDOW_SIZE;
    }

    // Allocate turbulence buffer
    turbulence_buffer_ = new (std::nothrow) float[window_size_];
    if (!turbulence_buffer_) {
        ESP_LOGE(TAG, "Failed to allocate turbulence buffer (%d elements)", window_size_);
    } else {
        std::memset(turbulence_buffer_, 0, window_size_ * sizeof(float));
    }

    // Initialize amplitude buffer
    std::memset(amplitude_buffer_, 0, sizeof(amplitude_buffer_));

    // Initialize filters (disabled by default)
    lowpass_filter_init(&lowpass_state_, LOWPASS_CUTOFF_DEFAULT, LOWPASS_SAMPLE_RATE, false);
    hampel_turbulence_init(&hampel_state_, HAMPEL_TURBULENCE_WINDOW_DEFAULT, HAMPEL_TURBULENCE_THRESHOLD_DEFAULT, false);
    breathing_filter_init(&breathing_filter_);
}

BaseDetector::~BaseDetector() {
    if (turbulence_buffer_) {
        delete[] turbulence_buffer_;
        turbulence_buffer_ = nullptr;
    }
}

BaseDetector::BaseDetector(BaseDetector&& other) noexcept
    : turbulence_buffer_(other.turbulence_buffer_)
    , num_amplitudes_(other.num_amplitudes_)
    , last_phase_turbulence_(other.last_phase_turbulence_)
    , last_ratio_turbulence_(other.last_ratio_turbulence_)
    , last_dser_(other.last_dser_)
    , last_plcr_(other.last_plcr_)
    , has_prev_phase_t_(other.has_prev_phase_t_)
    , buffer_index_(other.buffer_index_)
    , buffer_count_(other.buffer_count_)
    , window_size_(other.window_size_)
    , running_mean_(other.running_mean_)
    , running_m2_(other.running_m2_)
    , state_(other.state_)
    , total_packets_(other.total_packets_)
    , packet_index_(other.packet_index_)
    , lowpass_state_(other.lowpass_state_)
    , hampel_state_(other.hampel_state_)
    , use_cv_normalization_(other.use_cv_normalization_)
    , idle_mean_turbulence_(other.idle_mean_turbulence_)
    , idle_mean_phase_turb_(other.idle_mean_phase_turb_)
    , idle_amplitude_baseline_(other.idle_amplitude_baseline_)
    , idle_baselines_initialized_(other.idle_baselines_initialized_)
    , smooth_history_(other.smooth_history_)
    , smooth_count_(other.smooth_count_) {

    // Copy amplitude buffer and per-subcarrier state arrays
    std::memcpy(amplitude_buffer_, other.amplitude_buffer_, sizeof(amplitude_buffer_));
    std::memcpy(csi_static_, other.csi_static_, sizeof(csi_static_));
    std::memcpy(csi_phase_prev_t_, other.csi_phase_prev_t_, sizeof(csi_phase_prev_t_));

    // Transfer ownership - null out source pointer
    other.turbulence_buffer_ = nullptr;
}

BaseDetector& BaseDetector::operator=(BaseDetector&& other) noexcept {
    if (this != &other) {
        // Free existing resources
        delete[] turbulence_buffer_;

        // Transfer all state
        turbulence_buffer_ = other.turbulence_buffer_;
        num_amplitudes_ = other.num_amplitudes_;
        last_phase_turbulence_ = other.last_phase_turbulence_;
        last_ratio_turbulence_ = other.last_ratio_turbulence_;
        last_dser_ = other.last_dser_;
        last_plcr_ = other.last_plcr_;
        has_prev_phase_t_ = other.has_prev_phase_t_;
        buffer_index_ = other.buffer_index_;
        buffer_count_ = other.buffer_count_;
        window_size_ = other.window_size_;
        running_mean_ = other.running_mean_;
        running_m2_ = other.running_m2_;
        state_ = other.state_;
        total_packets_ = other.total_packets_;
        packet_index_ = other.packet_index_;
        lowpass_state_ = other.lowpass_state_;
        hampel_state_ = other.hampel_state_;
        use_cv_normalization_ = other.use_cv_normalization_;
        idle_mean_turbulence_ = other.idle_mean_turbulence_;
        idle_mean_phase_turb_ = other.idle_mean_phase_turb_;
        idle_amplitude_baseline_ = other.idle_amplitude_baseline_;
        idle_baselines_initialized_ = other.idle_baselines_initialized_;
        smooth_history_ = other.smooth_history_;
        smooth_count_ = other.smooth_count_;

        // Copy amplitude buffer and per-subcarrier state arrays
        std::memcpy(amplitude_buffer_, other.amplitude_buffer_, sizeof(amplitude_buffer_));
        std::memcpy(csi_static_, other.csi_static_, sizeof(csi_static_));
        std::memcpy(csi_phase_prev_t_, other.csi_phase_prev_t_, sizeof(csi_phase_prev_t_));

        // Transfer ownership - null out source pointer
        other.turbulence_buffer_ = nullptr;
    }
    return *this;
}

// ============================================================================
// VIRTUAL INTERFACE IMPLEMENTATION
// ============================================================================

void BaseDetector::process_packet(const int8_t* csi_data, size_t csi_len,
                                   const uint8_t* selected_subcarriers,
                                   uint8_t num_subcarriers) {
    if (!csi_data || !turbulence_buffer_) {
        ESP_LOGE(TAG, "process_packet: NULL pointer");
        return;
    }

    // Store amplitudes for feature extraction (before computing turbulence)
    // HT20: 64 subcarriers max
    int total_subcarriers = static_cast<int>(csi_len / 2);
    num_amplitudes_ = 0;

    // Phase differences buffer (one per subcarrier pair)
    float phase_diffs[MAX_AMPLITUDE_BUFFER];
    uint8_t num_phase_diffs = 0;
    float prev_phase = 0.0f;
    bool has_prev_phase = false;

    // DSER / PLCR accumulators (Uni-Fi, arXiv 2601.10980)
    constexpr float DSER_STATIC_ALPHA = 0.01f;  // ~100-packet EMA for H_s
    constexpr float DSER_EPS = 1e-3f;           // log argument floor
    constexpr float TWO_PI = 6.28318530718f;
    float dser_sum = 0.0f;
    uint8_t num_dser = 0;
    float dphase_sq_sum = 0.0f;
    uint8_t num_dphase = 0;

    if (selected_subcarriers && num_subcarriers > 0) {
        for (int i = 0; i < num_subcarriers && num_amplitudes_ < MAX_AMPLITUDE_BUFFER; i++) {
            int sc_idx = selected_subcarriers[i];
            if (sc_idx >= total_subcarriers) continue;

            // Espressif CSI format: [Imaginary, Real, ...] per subcarrier
            float Q = static_cast<float>(csi_data[sc_idx * 2]);      // Imaginary first
            float I = static_cast<float>(csi_data[sc_idx * 2 + 1]);  // Real second
            float amp = std::sqrt(I * I + Q * Q);
            amplitude_buffer_[num_amplitudes_] = amp;

            // DSER: slow EMA of amplitude → static channel H_s; residual is dynamic H_d
            float h_s = csi_static_[num_amplitudes_];
            if (h_s < 1e-6f) {
                h_s = amp;  // bootstrap on first packet for this slot
            } else {
                h_s = DSER_STATIC_ALPHA * amp + (1.0f - DSER_STATIC_ALPHA) * h_s;
            }
            csi_static_[num_amplitudes_] = h_s;
            float h_d = amp - h_s;
            float num = h_d * h_d + DSER_EPS;
            float den = h_s * h_s + DSER_EPS;
            dser_sum += std::log(num / den);
            num_dser++;

            // PLCR: packet-to-packet phase delta (same subcarrier) → path velocity
            float phase = std::atan2(Q, I);
            if (has_prev_phase_t_) {
                float dphase = phase - csi_phase_prev_t_[num_amplitudes_];
                // Unwrap to [-π, π]
                while (dphase > 3.14159265f) dphase -= TWO_PI;
                while (dphase < -3.14159265f) dphase += TWO_PI;
                dphase_sq_sum += dphase * dphase;
                num_dphase++;
            }
            csi_phase_prev_t_[num_amplitudes_] = phase;

            num_amplitudes_++;

            // Accumulate inter-subcarrier phase differences
            // Differencing adjacent subcarriers cancels common-mode phase rotation (HW artifact)
            if (has_prev_phase && num_phase_diffs < HT20_SELECTED_BAND_SIZE) {
                phase_diffs[num_phase_diffs++] = phase - prev_phase;
            }
            prev_phase = phase;
            has_prev_phase = true;
        }
        has_prev_phase_t_ = true;
    }

    // DSER: mean log-ratio across subcarriers (Uni-Fi eq. 3, averaged)
    last_dser_ = (num_dser > 0) ? dser_sum / num_dser : 0.0f;
    // PLCR: RMS of phase deltas normalized by 2π (path-length change rate proxy, eq. 4)
    last_plcr_ = (num_dphase > 0) ? std::sqrt(dphase_sq_sum / num_dphase) / TWO_PI : 0.0f;

    // Phase turbulence = std of inter-subcarrier phase differences
    last_phase_turbulence_ = (num_phase_diffs > 1)
        ? std::sqrt(calculate_variance_two_pass(phase_diffs, num_phase_diffs))
        : 0.0f;

    // SA-WiSense: subcarrier ratio turbulence (noise cancellation)
    // Ratio of adjacent subcarrier amplitudes cancels common-mode gain (AGC/interference)
    if (num_amplitudes_ > 1) {
        float ratios[HT20_SELECTED_BAND_SIZE];
        uint8_t num_ratios = 0;
        for (uint8_t i = 0; i + 1 < num_amplitudes_; i++) {
            if (amplitude_buffer_[i + 1] > 0.1f) {  // avoid div-by-zero
                ratios[num_ratios++] = amplitude_buffer_[i] / amplitude_buffer_[i + 1];
            }
        }
        last_ratio_turbulence_ = (num_ratios > 1)
            ? std::sqrt(calculate_variance_two_pass(ratios, num_ratios))
            : 0.0f;
    } else {
        last_ratio_turbulence_ = 0.0f;
    }

    // Calculate spatial turbulence
    // Two modes:
    //   CV normalization (std/mean): gain-invariant, used when gain is NOT locked
    //   Raw std: better sensitivity for contiguous bands, used when gain IS locked
    float turbulence = 0.0f;
    if (num_amplitudes_ > 0) {
        float variance = calculate_variance_two_pass(amplitude_buffer_, num_amplitudes_);
        turbulence = calculate_turbulence_from_variance(variance, amplitude_buffer_,
                                                         num_amplitudes_, use_cv_normalization_);
    }

    // Breathing bandpass: filter amplitude_sum at packet rate
    float amp_sum = 0.0f;
    for (uint8_t i = 0; i < num_amplitudes_; i++) amp_sum += amplitude_buffer_[i];
    float breath_filtered = breathing_filter_apply(&breathing_filter_, amp_sum);

    // Downsample filtered output for BPM estimation
    if (++bpm_downsample_cnt_ >= BPM_DOWNSAMPLE) {
        bpm_downsample_cnt_ = 0;
        bpm_buf_[bpm_buf_idx_]  = breath_filtered;
        bpm_time_buf_[bpm_buf_idx_] = static_cast<uint32_t>(esp_timer_get_time() / 1000u);  // ms
        bpm_buf_idx_ = (bpm_buf_idx_ + 1) % BPM_BUF_SIZE;
        if (bpm_buf_count_ < BPM_BUF_SIZE) bpm_buf_count_++;
        if (bpm_buf_count_ >= 16) compute_bpm_();
    }

    // Add to buffer with filtering
    add_turbulence_to_buffer(turbulence);
}

void BaseDetector::reset() {
    state_ = MotionState::IDLE;
    packet_index_ = 0;
    total_packets_ = 0;
    smooth_history_ = 0;
    smooth_count_ = 0;

    // Don't clear buffer - preserve "warm" state
}

// ============================================================================
// FILTER CONFIGURATION
// ============================================================================

void BaseDetector::configure_lowpass(bool enabled, float cutoff_hz) {
    lowpass_filter_init(&lowpass_state_, cutoff_hz, LOWPASS_SAMPLE_RATE, enabled);
    ESP_LOGI(TAG, "Low-pass filter %s (cutoff=%.1f Hz)", enabled ? "enabled" : "disabled", cutoff_hz);
}

void BaseDetector::configure_hampel(bool enabled, uint8_t window_size, float threshold) {
    hampel_turbulence_init(&hampel_state_, window_size, threshold, enabled);
    ESP_LOGI(TAG, "Hampel filter %s (window=%d, threshold=%.1f)",
             enabled ? "enabled" : "disabled", window_size, threshold);
}

void BaseDetector::set_cv_normalization(bool enabled) {
    use_cv_normalization_ = enabled;
    ESP_LOGI(TAG, "CV normalization %s", enabled ? "enabled (std/mean)" : "disabled (raw std)");
}

void BaseDetector::clear_buffer() {
    if (turbulence_buffer_) {
        std::memset(turbulence_buffer_, 0, window_size_ * sizeof(float));
    }
    buffer_index_ = 0;
    buffer_count_ = 0;
    running_mean_ = 0.0f;
    running_m2_ = 0.0f;
    state_ = MotionState::IDLE;
    smooth_history_ = 0;
    smooth_count_ = 0;

    // Reset DSER/PLCR per-subcarrier state
    std::memset(csi_static_, 0, sizeof(csi_static_));
    std::memset(csi_phase_prev_t_, 0, sizeof(csi_phase_prev_t_));
    has_prev_phase_t_ = false;
    last_dser_ = 0.0f;
    last_plcr_ = 0.0f;

    // Reset filters
    lowpass_filter_reset(&lowpass_state_);
    hampel_turbulence_init(&hampel_state_, hampel_state_.window_size,
                           hampel_state_.threshold, hampel_state_.enabled);

    ESP_LOGD(TAG, "Buffer cleared");
}

// ============================================================================
// BUFFER ACCESSORS
// ============================================================================

float BaseDetector::get_last_turbulence() const {
    if (!turbulence_buffer_ || buffer_count_ == 0) {
        return 0.0f;
    }

    int16_t last_idx = static_cast<int16_t>(buffer_index_) - 1;
    if (last_idx < 0) {
        last_idx = window_size_ - 1;
    }

    return turbulence_buffer_[last_idx];
}

float BaseDetector::get_running_variance() const {
    if (buffer_count_ < window_size_) {
        return 0.0f;
    }
    return running_m2_ / window_size_;
}

float BaseDetector::get_mean_turbulence() const {
    if (buffer_count_ == 0) {
        return 0.0f;
    }
    return running_mean_;
}

// ============================================================================
// PROTECTED METHODS
// ============================================================================

void BaseDetector::add_turbulence_to_buffer(float turbulence) {
    // Apply Hampel filter to remove outliers
    float hampel_filtered = hampel_filter_turbulence(&hampel_state_, turbulence);

    // Apply low-pass filter for noise reduction
    float new_val = lowpass_filter_apply(&lowpass_state_, hampel_filtered);

    if (buffer_count_ < window_size_) {
        // Buffer filling phase: standard Welford's accumulation
        turbulence_buffer_[buffer_index_] = new_val;
        buffer_count_++;
        float delta = new_val - running_mean_;
        running_mean_ += delta / buffer_count_;
        float delta2 = new_val - running_mean_;
        running_m2_ += delta * delta2;
    } else {
        // Buffer full: sliding window Welford's (replace oldest value)
        float old_val = turbulence_buffer_[buffer_index_];
        turbulence_buffer_[buffer_index_] = new_val;
        float new_mean = running_mean_ + (new_val - old_val) / window_size_;
        running_m2_ += (new_val - old_val) * (new_val - new_mean + old_val - running_mean_);
        // Clamp M2 to prevent negative drift from floating-point accumulation
        if (running_m2_ < 0.0f) running_m2_ = 0.0f;
        running_mean_ = new_mean;
    }

    buffer_index_ = (buffer_index_ + 1) % window_size_;
    packet_index_++;
    total_packets_++;
}

// ============================================================================
// TEMPORAL SMOOTHING (N/M VOTING)
// ============================================================================

MotionState BaseDetector::apply_temporal_smoothing(bool raw_motion) {
    // Shift history and add new decision
    smooth_history_ = ((smooth_history_ << 1) | (raw_motion ? 1 : 0)) & ((1 << SMOOTH_WINDOW) - 1);
    if (smooth_count_ < SMOOTH_WINDOW) smooth_count_++;

    // Count motion bits in history
    uint8_t motion_count = 0;
    uint8_t h = smooth_history_;
    for (uint8_t i = 0; i < smooth_count_; i++) {
        motion_count += (h & 1);
        h >>= 1;
    }

    // Asymmetric thresholds
    if (state_ == MotionState::IDLE) {
        if (motion_count >= SMOOTH_ENTER && smooth_count_ >= SMOOTH_ENTER) {
            return MotionState::MOTION;
        }
    } else {
        uint8_t idle_count = smooth_count_ - motion_count;
        if (idle_count >= SMOOTH_EXIT && smooth_count_ >= SMOOTH_EXIT) {
            return MotionState::IDLE;
        }
    }
    return state_;  // No change
}

// ============================================================================
// IDLE-GATED BASELINES
// ============================================================================

void BaseDetector::update_idle_baselines(float turbulence, float phase_turb, float amplitude_sum) {
    if (state_ != MotionState::IDLE || buffer_count_ < window_size_) {
        return;
    }

    float alpha = 1.0f / static_cast<float>(window_size_);

    if (!idle_baselines_initialized_) {
        idle_mean_turbulence_ = turbulence;
        idle_mean_phase_turb_ = phase_turb;
        idle_amplitude_baseline_ = amplitude_sum;
        idle_baselines_initialized_ = true;
    } else {
        idle_mean_turbulence_ = alpha * turbulence + (1.0f - alpha) * idle_mean_turbulence_;
        idle_mean_phase_turb_ = alpha * phase_turb + (1.0f - alpha) * idle_mean_phase_turb_;
        idle_amplitude_baseline_ = alpha * amplitude_sum + (1.0f - alpha) * idle_amplitude_baseline_;
    }
}

float BaseDetector::get_idle_mean_turbulence() const {
    return idle_baselines_initialized_ ? idle_mean_turbulence_ : running_mean_;
}

float BaseDetector::get_idle_mean_phase_turbulence() const {
    return idle_baselines_initialized_ ? idle_mean_phase_turb_ : last_phase_turbulence_;
}

float BaseDetector::get_idle_amplitude_baseline() const {
    return idle_amplitude_baseline_;
}

float BaseDetector::get_breathing_score() const {
    return breathing_filter_get_score(&breathing_filter_);
}

// ============================================================================
// COMPOSITE SCORE
// ============================================================================

float BaseDetector::get_composite_score() const {
    if (!idle_baselines_initialized_) {
        return 0.0f;
    }

    // Weights for each component (sum = 1.0)
    constexpr float W_TURB = 0.35f;
    constexpr float W_PHASE = 0.25f;
    constexpr float W_RATIO = 0.20f;
    constexpr float W_BREATH = 0.20f;

    // Turbulence deviation: how far current mean is from idle baseline
    float idle_turb = idle_mean_turbulence_;
    float turb_dev = 0.0f;
    if (idle_turb > 1e-6f) {
        turb_dev = (running_mean_ - idle_turb) / idle_turb;
        if (turb_dev < 0.0f) turb_dev = 0.0f;
    }

    // Phase turbulence deviation from idle baseline
    float idle_phase = idle_mean_phase_turb_;
    float phase_dev = 0.0f;
    if (idle_phase > 1e-6f) {
        phase_dev = (last_phase_turbulence_ - idle_phase) / idle_phase;
        if (phase_dev < 0.0f) phase_dev = 0.0f;
    }

    // Ratio turbulence (absolute — no baseline needed, ~0 when idle)
    float ratio_dev = last_ratio_turbulence_;

    // Breathing score (absolute energy)
    float breath = breathing_filter_get_score(&breathing_filter_);

    // Weighted sum — all components are ≥ 0, unbounded above
    return W_TURB * turb_dev + W_PHASE * phase_dev +
           W_RATIO * ratio_dev + W_BREATH * breath;
}

// ============================================================================
// BREATHING RATE BPM ESTIMATION
// ============================================================================

void BaseDetector::compute_bpm_() {
    const uint8_t n = bpm_buf_count_;

    // Check signal energy: skip if too quiet (empty room / strong motion)
    float rms = 0.0f;
    for (uint8_t i = 0; i < n; i++) rms += bpm_buf_[i] * bpm_buf_[i];
    rms = sqrtf(rms / n);
    if (rms < BPM_MIN_RMS) {
        breathing_rate_bpm_ = 0.0f;
        return;
    }

    // Compute actual sample rate from timestamps (oldest→newest span)
    const uint8_t oldest = (n < BPM_BUF_SIZE) ? 0 : bpm_buf_idx_;
    const uint8_t newest = (oldest + n - 1) % BPM_BUF_SIZE;
    uint32_t t_oldest = bpm_time_buf_[oldest];
    uint32_t t_newest = bpm_time_buf_[newest];
    uint32_t span_ms  = (t_newest >= t_oldest) ? (t_newest - t_oldest) : 1u;
    if (span_ms < 100u) {
        breathing_rate_bpm_ = 0.0f;  // too short to estimate
        return;
    }
    // fs = (n-1) intervals / span_s  — samples per second
    float fs = static_cast<float>(n - 1) / (static_cast<float>(span_ms) * 0.001f);
    if (fs < 0.1f) fs = 0.1f;  // sanity clamp

    // DFT at candidate breathing rates (6-30 BPM, step 2)
    static constexpr float TEST_BPM[] = {6, 8, 10, 12, 14, 16, 18, 20, 22, 24, 26, 28, 30};
    static constexpr uint8_t NUM_TEST = 13;

    float best_bpm    = 0.0f;
    float best_mag_sq = 0.0f;

    for (uint8_t b = 0; b < NUM_TEST; b++) {
        float freq_hz = TEST_BPM[b] / 60.0f;
        float re = 0.0f, im = 0.0f;
        for (uint8_t i = 0; i < n; i++) {
            uint8_t idx = (oldest + i) % BPM_BUF_SIZE;
            // Use actual elapsed time for each sample instead of uniform index
            uint32_t t_i = bpm_time_buf_[idx];
            float t_s = static_cast<float>((t_i >= t_oldest) ? (t_i - t_oldest) : 0u) * 0.001f;
            float angle = 2.0f * M_PI * freq_hz * t_s;
            re += bpm_buf_[idx] * cosf(angle);
            im += bpm_buf_[idx] * sinf(angle);
        }
        float mag_sq = re * re + im * im;
        if (mag_sq > best_mag_sq) {
            best_mag_sq = mag_sq;
            best_bpm    = TEST_BPM[b];
        }
    }

    breathing_rate_bpm_ = best_bpm;
}

}  // namespace espectre
}  // namespace esphome
