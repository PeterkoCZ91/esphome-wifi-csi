/*
 * ESPectre - Base Detector
 *
 * Abstract base class for motion detection algorithms.
 * Provides shared turbulence buffer management and filtering.
 *
 * Author: Francesco Pace <francesco.pace@gmail.com>
 * License: GPLv3
 */

#pragma once

#include <cstdint>
#include <cstddef>
#include "filters.h"
#include "utils.h"

namespace esphome {
namespace espectre {

// ============================================================================
// MOTION STATE
// ============================================================================

enum class MotionState {
    IDLE,       // No motion detected
    MOTION      // Motion in progress
};

// ============================================================================
// DETECTOR CONSTANTS
// ============================================================================

constexpr uint16_t DETECTOR_DEFAULT_WINDOW_SIZE = 75;
constexpr uint16_t DETECTOR_MIN_WINDOW_SIZE = 10;
constexpr uint16_t DETECTOR_MAX_WINDOW_SIZE = 200;

// Calibration buffer size = 10 windows worth of packets
constexpr uint16_t CALIBRATION_NUM_WINDOWS = 10;
constexpr uint16_t CALIBRATION_DEFAULT_BUFFER_SIZE = DETECTOR_DEFAULT_WINDOW_SIZE * CALIBRATION_NUM_WINDOWS;

// ============================================================================
// BASE DETECTOR CLASS
// ============================================================================

/**
 * Abstract base class for motion detection algorithms
 *
 * Provides shared functionality:
 * - Turbulence buffer management (circular buffer)
 * - Hampel and low-pass filtering
 * - CSI processing and spatial turbulence calculation
 * - Amplitude storage for feature extraction
 *
 * Subclasses must implement:
 * - update_state(): detection algorithm logic
 * - get_motion_metric(): primary detection metric
 * - get_threshold() / set_threshold(): threshold management
 * - get_name(): detector name for logging
 */
class BaseDetector {
public:
    /**
     * Constructor
     *
     * @param window_size Buffer window size (10-200 packets)
     */
    explicit BaseDetector(uint16_t window_size = DETECTOR_DEFAULT_WINDOW_SIZE);

    virtual ~BaseDetector();

    // Move semantics (Rule of Five - we manage raw pointer)
    BaseDetector(BaseDetector&& other) noexcept;
    BaseDetector& operator=(BaseDetector&& other) noexcept;

    // Disable copy (raw pointer ownership)
    BaseDetector(const BaseDetector&) = delete;
    BaseDetector& operator=(const BaseDetector&) = delete;

    // ========================================================================
    // VIRTUAL INTERFACE (implemented in base)
    // ========================================================================

    /**
     * Process a CSI packet and update internal state
     *
     * Calculates spatial turbulence from CSI data, applies filtering,
     * and stores in circular buffer. Also stores amplitudes for feature
     * extraction by ML detector.
     *
     * @param csi_data Raw CSI data (I/Q interleaved)
     * @param csi_len Length of CSI data
     * @param selected_subcarriers Array of subcarrier indices
     * @param num_subcarriers Number of selected subcarriers
     */
    virtual void process_packet(const int8_t* csi_data, size_t csi_len,
                                const uint8_t* selected_subcarriers = nullptr,
                                uint8_t num_subcarriers = 0);

    /**
     * Reset detector state
     *
     * Resets state machine but preserves buffer ("warm" restart).
     */
    virtual void reset();

    /**
     * Get current motion state
     */
    virtual MotionState get_state() const { return state_; }

    /**
     * Check if detector is ready (buffer filled)
     */
    virtual bool is_ready() const { return buffer_count_ >= window_size_; }

    /**
     * Get total packets processed
     */
    virtual uint32_t get_total_packets() const { return total_packets_; }

    // ========================================================================
    // PURE VIRTUAL INTERFACE (must be implemented by subclasses)
    // ========================================================================

    /**
     * Update state machine (call at publish interval)
     *
     * Subclasses implement their detection algorithm here.
     */
    virtual void update_state() = 0;

    /**
     * Get current motion metric value
     *
     * @return Primary metric (moving variance for MVS, probability for ML)
     */
    virtual float get_motion_metric() const = 0;

    /**
     * Set detection threshold
     *
     * @param threshold New threshold value
     * @return true if value was accepted
     */
    virtual bool set_threshold(float threshold) = 0;

    /**
     * Get current threshold
     */
    virtual float get_threshold() const = 0;

    /**
     * Set hysteresis factor for asymmetric IDLE↔MOTION transitions
     *
     * @param factor Hysteresis multiplier (0.3-1.0, applied to threshold for MOTION→IDLE)
     * @return true if value was accepted
     */
    virtual bool set_hysteresis(float factor) { return false; }

    /**
     * Get current hysteresis factor
     */
    virtual float get_hysteresis_factor() const { return 1.0f; }

    /**
     * Get detector name for logging
     */
    virtual const char* get_name() const = 0;

    // ========================================================================
    // FILTER CONFIGURATION
    // ========================================================================

    /**
     * Configure low-pass filter
     *
     * @param enabled Whether to enable the filter
     * @param cutoff_hz Cutoff frequency (5.0-20.0 Hz)
     */
    void configure_lowpass(bool enabled, float cutoff_hz = LOWPASS_CUTOFF_DEFAULT);

    /**
     * Configure Hampel filter
     *
     * @param enabled Whether to enable the filter
     * @param window_size Window size (3-11)
     * @param threshold MAD multiplier threshold
     */
    void configure_hampel(bool enabled, uint8_t window_size = HAMPEL_TURBULENCE_WINDOW_DEFAULT,
                          float threshold = HAMPEL_TURBULENCE_THRESHOLD_DEFAULT);

    /**
     * Configure CV normalization mode
     *
     * CV normalization (std/mean) makes turbulence gain-invariant but reduces
     * sensitivity for contiguous subcarrier bands (P95). When gain is locked,
     * raw std is preferred as amplitudes are already stable.
     *
     * @param enabled true = CV normalization (std/mean), false = raw std
     */
    void set_cv_normalization(bool enabled);

    /**
     * Check if CV normalization is enabled
     */
    bool is_cv_normalization_enabled() const { return use_cv_normalization_; }

    /**
     * Clear turbulence buffer (cold restart)
     */
    void clear_buffer();

    // ========================================================================
    // BUFFER ACCESSORS (for subclasses and feature extraction)
    // ========================================================================

    /**
     * Get turbulence buffer pointer
     */
    const float* get_turbulence_buffer() const { return turbulence_buffer_; }

    /**
     * Get number of valid samples in buffer
     */
    uint16_t get_buffer_count() const { return buffer_count_; }

    /**
     * Get configured window size
     */
    uint16_t get_window_size() const { return window_size_; }

    /**
     * Get last packet amplitudes (for feature extraction)
     */
    const float* get_last_amplitudes() const { return amplitude_buffer_; }

    /**
     * Get number of amplitudes stored
     */
    uint8_t get_num_amplitudes() const { return num_amplitudes_; }

    /**
     * Get last turbulence value
     */
    float get_last_turbulence() const;

    /**
     * Get running variance (O(1), maintained incrementally via Welford's algorithm)
     */
    float get_running_variance() const;

    /**
     * Get mean turbulence over the buffer window
     */
    float get_mean_turbulence() const;

    /**
     * Get last phase turbulence value
     *
     * Std of adjacent inter-subcarrier phase differences per packet.
     * Cancels common-mode phase rotation (hardware artifact) by differencing.
     * Sensitive to slow motion (breathing, fine movement) that amplitude misses.
     */
    float get_last_phase_turbulence() const { return last_phase_turbulence_; }

    /**
     * Get last subcarrier ratio turbulence (SA-WiSense noise cancellation)
     *
     * Std of amplitude ratios between adjacent subcarrier pairs.
     * Cancels common-mode gain variations (AGC, interference) because
     * neighboring subcarriers share the same hardware gain.
     */
    float get_last_ratio_turbulence() const { return last_ratio_turbulence_; }

    /**
     * Get last Dynamic-to-Static Energy Ratio (Uni-Fi DSER, arXiv 2601.10980 eq. 3)
     *
     * DSER(k,t) = log(|H_d(k,t)|² / |H_s(k,t)|²), averaged over subcarriers.
     * H_s is slow EMA of amplitude per subcarrier (static channel),
     * H_d = amplitude − H_s is the dynamic residual. Large negative in absence,
     * approaches 0 when motion dominates.
     */
    float get_last_dser() const { return last_dser_; }

    /**
     * Get last Path-Length Change Rate proxy (Uni-Fi PLCR, arXiv 2601.10980 eq. 4)
     *
     * RMS of per-subcarrier packet-to-packet phase deltas (unwrapped to [−π,π]),
     * normalized by 2π. Proportional to instantaneous Doppler / path velocity.
     * 0 when stationary, grows with movement through the channel.
     */
    float get_last_plcr() const { return last_plcr_; }

    /**
     * Get breathing score (RMS of bandpass-filtered amplitude, 0.08-0.6 Hz)
     *
     * Elevated when periodic variation in amplitude matches breathing range (6-30 BPM).
     * Useful for detecting stationary presence (person sitting/sleeping).
     */
    float get_breathing_score() const;

    /**
     * Get estimated breathing rate in breaths-per-minute (approximate, ±2 BPM).
     * Returns 0 when signal energy is too low (empty room or strong motion).
     * Requires ~16s of data to produce first estimate.
     */
    float get_breathing_rate_bpm() const { return breathing_rate_bpm_; }

    /**
     * Get idle-gated mean turbulence (EMA, updated only during IDLE state)
     * Falls back to running_mean_ if not yet initialized.
     */
    float get_idle_mean_turbulence() const;

    /**
     * Get idle-gated mean phase turbulence (EMA, updated only during IDLE state)
     */
    float get_idle_mean_phase_turbulence() const;

    /**
     * Get idle-gated amplitude baseline (EMA, updated only during IDLE state)
     */
    float get_idle_amplitude_baseline() const;

    /**
     * Check if idle baselines have been initialized (at least one IDLE update)
     */
    bool are_idle_baselines_initialized() const { return idle_baselines_initialized_; }

    /**
     * Get composite presence score (0.0 = empty room, higher = more likely occupied)
     *
     * Weighted combination of deviations from idle baselines:
     *   w1×turb_dev + w2×phase_dev + w3×ratio_turb + w4×breathing
     * All components normalized relative to their idle baselines.
     * Returns 0 if baselines not yet initialized.
     */
    float get_composite_score() const;

    /**
     * Check if low-pass filter is enabled
     */
    bool is_lowpass_enabled() const { return lowpass_state_.enabled; }

    /**
     * Check if Hampel filter is enabled
     */
    bool is_hampel_enabled() const { return hampel_state_.enabled; }

protected:
    /**
     * Add turbulence value to buffer (with filtering)
     */
    void add_turbulence_to_buffer(float turbulence);

    /**
     * Update idle-gated baselines (call after update_state() when state is known)
     * Only updates when state_==IDLE and buffer is full.
     */
    void update_idle_baselines(float turbulence, float phase_turb, float amplitude_sum);

    // Buffer state
    float* turbulence_buffer_;
    float amplitude_buffer_[MAX_AMPLITUDE_BUFFER];  // Last packet amplitudes (HT20: 12, HT40: 12)
    uint8_t num_amplitudes_;
    float last_phase_turbulence_{0.0f};  // Std of inter-subcarrier phase diffs (last packet)
    float last_ratio_turbulence_{0.0f}; // Std of adjacent subcarrier amplitude ratios (SA-WiSense)
    float last_dser_{0.0f};              // Uni-Fi Dynamic-to-Static Energy Ratio
    float last_plcr_{0.0f};              // Uni-Fi Path-Length Change Rate proxy

    // Per-subcarrier state for DSER (slow EMA of amplitude = static channel H_s)
    float csi_static_[MAX_AMPLITUDE_BUFFER]{0.0f};

    // Per-subcarrier state for PLCR (previous packet's phase for temporal diff)
    float csi_phase_prev_t_[MAX_AMPLITUDE_BUFFER]{0.0f};
    bool has_prev_phase_t_{false};
    uint16_t buffer_index_;
    uint16_t buffer_count_;
    uint16_t window_size_;

    // Incremental variance (Welford's sliding window) — O(1) per update
    float running_mean_{0.0f};
    float running_m2_{0.0f};  // Sum of squared deviations from mean

    // Motion state
    MotionState state_;
    uint32_t total_packets_;
    uint32_t packet_index_;

    // Filters
    hampel_filter_state_t hampel_state_;
    lowpass_filter_state_t lowpass_state_;

    // CV normalization: true = std/mean (gain-invariant), false = raw std
    // Default false: raw std is more sensitive and matches ML model training
    // Set true only for chips without gain lock (e.g., ESP32)
    bool use_cv_normalization_{false};

    // Idle-gated baselines (EMA, O(1) memory)
    float idle_mean_turbulence_{0.0f};
    float idle_mean_phase_turb_{0.0f};
    float idle_amplitude_baseline_{0.0f};
    bool idle_baselines_initialized_{false};

    // Breathing bandpass filter (0.08-0.6 Hz on amplitude_sum, per-packet)
    breathing_filter_state_t breathing_filter_{};

    // Breathing rate BPM estimation (DFT on downsampled bandpass output)
    static constexpr uint8_t  BPM_BUF_SIZE   = 64;    // samples (~16-64s depending on packet rate)
    static constexpr uint8_t  BPM_DOWNSAMPLE = 50;    // sample every N packets
    static constexpr float    BPM_MIN_RMS    = 0.008f;
    float    bpm_buf_[BPM_BUF_SIZE]{};
    uint32_t bpm_time_buf_[BPM_BUF_SIZE]{};  // ms timestamps per sample (for actual fs)
    uint8_t  bpm_buf_idx_{0};
    uint8_t  bpm_buf_count_{0};
    uint8_t  bpm_downsample_cnt_{0};
    float   breathing_rate_bpm_{0.0f};
    void    compute_bpm_();

    // Temporal smoothing: require N of last M raw decisions to agree
    static constexpr uint8_t SMOOTH_WINDOW = 6;
    static constexpr uint8_t SMOOTH_ENTER = 4;   // 4/6 for IDLE→MOTION
    static constexpr uint8_t SMOOTH_EXIT = 5;     // 5/6 for MOTION→IDLE
    uint8_t smooth_history_{0};  // Bitmask of last SMOOTH_WINDOW raw decisions (1=motion)
    uint8_t smooth_count_{0};    // Number of samples in history

    /**
     * Apply N/M temporal smoothing to raw motion decision
     *
     * Maintains a sliding bitmask of last SMOOTH_WINDOW raw decisions.
     * Uses asymmetric thresholds: SMOOTH_ENTER to enter MOTION,
     * SMOOTH_EXIT to return to IDLE.
     *
     * @param raw_motion Raw threshold-crossing decision
     * @return Smoothed motion state
     */
    MotionState apply_temporal_smoothing(bool raw_motion);
};

}  // namespace espectre
}  // namespace esphome
