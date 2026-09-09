/*
 * Combined Example: IMU Acceleration + Wi-Fi + SNTP + SD Card Logging + LVGL UI
 *
 * Features:
 *  - QMA6100P accelerometer reading (from display_rotation)
 *  - Wi-Fi STA connection (from tcp_server)
 *  - SNTP time sync (from tcp_server)
 *  - Button A start/stop SD card CSV logging (from data_capture_sim)
 *  - LVGL real-time display (from data_capture_sim)
 *  - 6-face calibration mode: +X, -X, +Y, -Y, +Z, -Z, 10s each
 *
 * Hardware: ESP32-S3-EYE (BSP)
 */

#include <assert.h>
#include <errno.h>
#include <inttypes.h>
#include <math.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>
#include <time.h>
#include <sys/time.h>

#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "bsp/esp-bsp.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_netif_sntp.h"
#include "esp_sntp.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "iot_button.h"
#include "lvgl.h"
#include "nvs_flash.h"
#include "qma6100p.h"
#include "sdmmc_cmd.h"

/* ================================================================
 *  Common Config
 * ================================================================ */
#define TARGET_SAMPLE_FREQ_HZ    100      /* Target: 100 Hz */
#define SAMPLE_PERIOD_MS         (1000 / TARGET_SAMPLE_FREQ_HZ)  /* 10 ms for 100 Hz */
#define START_BUTTON_INDEX       BSP_BUTTON_1
#define UI_TEXT_LEN              96
#define FILE_PATH_LEN            128
#define FREQ_MEASURE_SAMPLES     100      /* Measure frequency over 100 samples */

/* Data format config */
#define GRAVITY_ACCEL            9.80665f /* 1 g = 9.80665 m/s² */
#define DATA_LABEL               "raw"    /* Default label for data */

/* 6-face calibration config */
#define FACE_DURATION_SEC        10       /* Each face: 10 seconds */
#define FACE_DURATION_MS         (FACE_DURATION_SEC * 1000)
#define STATIONARY_THRESHOLD     0.5f     /* m/s² - max variance to be considered stationary */
#define STATIONARY_SAMPLES       50       /* Number of samples to check for stationary */

/* Stand action protocol config */
#define STAND_PREP_DURATION_SEC           3    /* 3s prep: hold stand pose */
#define STAND_EXECUTE_DURATION_MIN_SEC    20   /* 20-30s stand execution per group */
#define STAND_EXECUTE_DURATION_MAX_SEC    30   /* 20-30s stand execution per group */
#define STAND_INTERVAL_DURATION_SEC       5    /* 5s interval - stationary idle */
#define STAND_TOTAL_GROUPS                3    /* 3 groups total */

/* Stand protocol file path buffer */
#define STAND_FILE_PATH_LEN               128

/* Stairs action protocol config */
#define STAIRS_PREP_DURATION_SEC          3    /* 3s prep: hold stairs pose */
#define STAIRS_EXECUTE_DURATION_MIN_SEC   20   /* 20-30s stairs execution per group */
#define STAIRS_EXECUTE_DURATION_MAX_SEC   30   /* 20-30s stairs execution per group */
#define STAIRS_INTERVAL_DURATION_SEC      60   /* 60s interval - stationary idle (changed from 5s) */
#define STAIRS_TOTAL_GROUPS               3    /* 3 groups total */

/* Stairs protocol file path buffer */
#define STAIRS_FILE_PATH_LEN              128

/* Bend action protocol config */
#define BEND_DURATION_MIN_SEC         20       /* 20-30s bend execution per group */
#define BEND_DURATION_MAX_SEC         30       /* 20-30s bend execution per group */
#define BEND_INTERVAL_MIN_SEC         60       /* 60s minimum interval between groups */
#define BEND_INTERVAL_MAX_SEC         90       /* 90s maximum interval between groups */
#define BEND_TOTAL_GROUPS             3        /* 3 groups total */

/* Jump action protocol config */
#define JUMP_EXECUTE_DURATION_MIN_SEC   5      /* 5-10s jump execution per group (3-5 jumps) */
#define JUMP_EXECUTE_DURATION_MAX_SEC   10     /* 5-10s jump execution per group (3-5 jumps) */
#define JUMP_INTERVAL_MIN_SEC           5      /* 5s minimum interval between groups */
#define JUMP_INTERVAL_MAX_SEC           10     /* 10s maximum interval between groups */
#define JUMP_TOTAL_GROUPS               3      /* 3 groups total */

/* Fall action protocol config */
#define FALL_EXECUTE_DURATION_MIN_SEC 5        /* 5-10s fall execution per group (multiple falls) */
#define FALL_EXECUTE_DURATION_MAX_SEC 10       /* 5-10s fall execution per group (multiple falls) */
#define FALL_INTERVAL_MIN_SEC         15       /* 15s minimum interval between groups */
#define FALL_INTERVAL_MAX_SEC         30       /* 30s maximum interval between groups */
#define FALL_TOTAL_GROUPS             3        /* 3 groups total */

/* File path buffer for current group */
#define GROUP_FILE_PATH_LEN           128

/* WiFi config — ★ 请修改为你的热点信息 */
#define WIFI_SSID                "421"
#define WIFI_PASSWORD            "1"
#define EXAMPLE_ESP_MAXIMUM_RETRY  5

#define WIFI_CONNECTED_BIT       BIT0
#define WIFI_FAIL_BIT            BIT1

/* Accelerometer config */
#define ACCEL_FILTER_ALPHA       0.18f

/* I2C port number for legacy driver (used by qma6100p library) */
#define QMA6100P_I2C_PORT        (i2c_port_t)CONFIG_BSP_I2C_NUM

static const char *TAG = "imu_logger";

/* ================================================================
 *  Global State
 * ================================================================ */
static SemaphoreHandle_t s_state_mutex;
static button_handle_t s_buttons[BSP_BUTTON_NUM] = {0};

/* LVGL UI objects */
static lv_obj_t *s_title_label;
static lv_obj_t *s_state_label;
static lv_obj_t *s_time_display;
static lv_obj_t *s_accel_x_label;
static lv_obj_t *s_accel_y_label;
static lv_obj_t *s_accel_z_label;
static lv_obj_t *s_samples_label;
static lv_obj_t *s_mode_label;
static lv_obj_t *s_status_bar;
static lv_obj_t *s_face_label;
static lv_obj_t *s_accel_mag_label;
static lv_obj_t *s_stand_label_ui;
static bool s_ui_ready = false;

/* State variables */
static bool s_sd_ready;
static bool s_collecting;
static bool s_wifi_connected;
static bool s_time_synced;
static FILE *s_data_file;
static float s_latest_accel_x;
static float s_latest_accel_y;
static float s_latest_accel_z;
static uint32_t s_sample_count;
static uint64_t s_base_timestamp_ms;          /* NTP-anchored base timestamp for sample #0 */
static char s_status_text[UI_TEXT_LEN];
static char s_file_path[FILE_PATH_LEN];
static char s_time_str[64];

/* WiFi event group */
static EventGroupHandle_t s_wifi_event_group;
static int s_retry_num;

/* Accelerometer handle */
static qma6100p_handle_t s_accel = NULL;

/* Accelerometer low-pass filter state (reset per capture session) */
static float s_filter_x = 0, s_filter_y = 0, s_filter_z = 0;
static bool s_filter_init = false;

/* Frequency measurement variables */
static uint32_t s_freq_sample_count = 0;
static int64_t s_freq_start_time_us = 0;
static bool s_freq_measuring = false;
static float s_measured_freq_hz = 0.0f;
static bool s_freq_valid = false;

/* 6-face calibration state */
typedef enum {
    FACE_IDLE = 0,
    FACE_POS_X,      /* +X face up */
    FACE_NEG_X,      /* -X face up */
    FACE_POS_Y,      /* +Y face up */
    FACE_NEG_Y,      /* -Y face up */
    FACE_POS_Z,      /* +Z face up */
    FACE_NEG_Z,      /* -Z face up */
    FACE_COMPLETE
} calibration_face_t;

static calibration_face_t s_current_face = FACE_IDLE;
static uint32_t s_face_sample_count = 0;
static int64_t s_face_start_time_ms = 0;
static FILE *s_calibration_file = NULL;
static char s_face_name[16] = "IDLE";

/* Stationary detection */
static float s_stationary_buffer_x[STATIONARY_SAMPLES];
static float s_stationary_buffer_y[STATIONARY_SAMPLES];
static float s_stationary_buffer_z[STATIONARY_SAMPLES];
static uint32_t s_stationary_index = 0;
static bool s_stationary_ready = false;

/* Calibration mode accumulators */
static float s_calib_sum_x[7] = {0};  /* Index by face enum */
static float s_calib_sum_y[7] = {0};
static float s_calib_sum_z[7] = {0};
static uint32_t s_calib_count[7] = {0};

/* Stand action protocol state */
typedef enum {
    STAND_PROTOCOL_IDLE = 0,
    STAND_PROTOCOL_PREP,       /* 3s prep: hold stand pose, label = "stand" */
    STAND_PROTOCOL_EXECUTE,    /* 20-30s execute: standing still, label = "stand" */
    STAND_PROTOCOL_INTERVAL,   /* 5s interval: relax, label = "idle" */
    STAND_PROTOCOL_COMPLETE    /* All 3 groups done */
} stand_protocol_state_t;

#define STAND_PREP_DURATION_SEC    3        /* 3s prep: hold stand pose */

static stand_protocol_state_t s_stand_state = STAND_PROTOCOL_IDLE;
static int s_stand_group = 0;                        /* Current group index (0-based) */
static int64_t s_stand_phase_start_ms = 0;           /* Phase start time (ms) */
static int s_stand_duration_sec = 25;                /* Current stand duration (20-30s) */
static int s_stand_interval_sec = 5;                 /* Current stand interval (5s) */
static char s_stand_label[16] = "";                  /* Current label for stand protocol */
static char s_stand_display[64] = "Stand: IDLE";     /* UI display text */
static bool s_stand_protocol_active = false;          /* Is stand protocol running? */
static FILE *s_stand_data_file = NULL;               /* Current group data file */
static bool s_stand_file_open = false;               /* Track if stand file is open */

/* Stairs action protocol state */
#define STAIRS_PREP_DURATION_SEC    3        /* 3s prep: hold stairs pose */
typedef enum {
    STAIRS_PROTOCOL_IDLE = 0,
    STAIRS_PROTOCOL_PREP,      /* 3s prep: hold stairs pose, label = "stairs" */
    STAIRS_PROTOCOL_EXECUTE,   /* 20-30s stairs execution: label = "stairs" */
    STAIRS_PROTOCOL_INTERVAL,  /* 60s rest: label = "idle" */
    STAIRS_PROTOCOL_COMPLETE   /* All 3 groups done */
} stairs_protocol_state_t;

static stairs_protocol_state_t s_stairs_state = STAIRS_PROTOCOL_IDLE;
static int s_stairs_group = 0;                        /* Current group index (0-based) */
static int64_t s_stairs_phase_start_ms = 0;           /* Phase start time (ms) */
static char s_stairs_label[16] = "";                   /* Current label for stairs protocol */
static char s_stairs_display[64] = "Stairs: IDLE";    /* UI display text */
static bool s_stairs_protocol_active = false;          /* Is stairs protocol running? */
static int s_stairs_duration_sec = 25;                 /* Current stairs duration (20-30s) */
static int s_stairs_interval_sec = 60;                 /* Current stairs interval (60s) */
static FILE *s_stairs_data_file = NULL;                /* Current group data file */

/* Bend action protocol state */
#define BEND_PREP_DURATION_SEC    3        /* 3s prep: hold bend pose */
typedef enum {
    BEND_PROTOCOL_IDLE = 0,
    BEND_PROTOCOL_PREP,      /* 3s prep: hold bend pose, label = "bend" */
    BEND_PROTOCOL_EXECUTE,   /* 20-30s bend execution: label = "bend" */
    BEND_PROTOCOL_INTERVAL,  /* 60-90s rest: label = "idle" */
    BEND_PROTOCOL_COMPLETE   /* All 3 groups done */
} bend_protocol_state_t;

static bend_protocol_state_t s_bend_state = BEND_PROTOCOL_IDLE;
static int s_bend_group = 0;                        /* Current group index (0-based) */
static int64_t s_bend_phase_start_ms = 0;           /* Phase start time (ms) */
static char s_bend_label[16] = "";                   /* Current label for bend protocol */
static char s_bend_display[64] = "Bend: IDLE";      /* UI display text */
static bool s_bend_protocol_active = false;          /* Is bend protocol running? */
static int s_bend_duration_sec = 25;                 /* Current bend duration (20-30s) */
static int s_bend_interval_duration_sec = 60;        /* Current interval duration (60-90s) */
static char s_bend_file_path[FILE_PATH_LEN];         /* Current group file path */
static FILE *s_bend_data_file = NULL;                /* Current group data file */

/* Jump action protocol state */
#define JUMP_PREP_DURATION_SEC    3        /* 3s prep: hold jump pose */

typedef enum {
    JUMP_PROTOCOL_IDLE = 0,
    JUMP_PROTOCOL_PREP,      /* 3s prep: hold jump pose, label = "jump" */
    JUMP_PROTOCOL_EXECUTE,   /* 5-10s jump execution: label = "jump" */
    JUMP_PROTOCOL_INTERVAL,  /* 5-10s rest: label = "idle" */
    JUMP_PROTOCOL_COMPLETE   /* All 3 groups done */
} jump_protocol_state_t;

static jump_protocol_state_t s_jump_state = JUMP_PROTOCOL_IDLE;
static int s_jump_group = 0;                        /* Current group index (0-based) */
static int64_t s_jump_phase_start_ms = 0;           /* Phase start time (ms) */
static char s_jump_label[16] = "";                   /* Current label for jump protocol */
static char s_jump_display[64] = "Jump: IDLE";      /* UI display text */
static bool s_jump_protocol_active = false;          /* Is jump protocol running? */
static int s_jump_duration_sec = 10;                 /* Current jump duration (5-10s) */
static int s_jump_interval_duration_sec = 5;         /* Current interval duration (5-10s) */
static char s_jump_file_path[FILE_PATH_LEN];         /* Current group file path */
static FILE *s_jump_data_file = NULL;                /* Current group data file */

/* Fall action protocol state */
#define FALL_PREP_DURATION_SEC    3        /* 3s prep: hold fall pose */
typedef enum {
    FALL_PROTOCOL_IDLE = 0,
    FALL_PROTOCOL_PREP,      /* 3s prep: hold fall pose, label = "fall" */
    FALL_PROTOCOL_EXECUTE,   /* 5-10s fall execution: label = "fall" */
    FALL_PROTOCOL_INTERVAL,  /* 15-30s rest: label = "idle" */
    FALL_PROTOCOL_COMPLETE   /* All 3 groups done */
} fall_protocol_state_t;

static fall_protocol_state_t s_fall_state = FALL_PROTOCOL_IDLE;
static int s_fall_group = 0;                        /* Current group index (0-based) */
static int64_t s_fall_phase_start_ms = 0;           /* Phase start time (ms) */
static char s_fall_label[16] = "";                   /* Current label for fall protocol */
static char s_fall_display[64] = "Fall: IDLE";      /* UI display text */
static bool s_fall_protocol_active = false;          /* Is fall protocol running? */
static int s_fall_duration_sec = 5;                  /* Current fall duration (5-10s) */
static int s_fall_interval_duration_sec = 15;        /* Current interval duration (15-30s) */
static char s_fall_file_path[FILE_PATH_LEN];         /* Current group file path */
static FILE *s_fall_data_file = NULL;                /* Current group data file */

/* Forward declaration */
static void refresh_ui(void);

/* ================================================================
 *  Utility
 * ================================================================ */
static void set_status_locked(const char *fmt, ...)
{
    va_list args;
    va_start(args, fmt);
    vsnprintf(s_status_text, sizeof(s_status_text), fmt, args);
    va_end(args);
}

static void refresh_ui(void)
{
    bool collecting = false;
    bool sd_ready = false;
    float ax = 0, ay = 0, az = 0;
    uint32_t sample_count = 0;
    char time_buf[64];
    char face_buf[32];

    bool freq_valid = false;
    float measured_freq = 0.0f;

    /* Do not touch LVGL objects before they are created */
    if (!s_ui_ready) {
        return;
    }

    /* Check if critical UI objects are initialized */
    if (!s_status_bar || !s_face_label) {
        return;
    }

    if (xSemaphoreTake(s_state_mutex, pdMS_TO_TICKS(50)) != pdTRUE) {
        return;
    }

    collecting = s_collecting;
    sd_ready = s_sd_ready;
    ax = s_latest_accel_x;
    ay = s_latest_accel_y;
    az = s_latest_accel_z;
    sample_count = s_sample_count;
    snprintf(time_buf, sizeof(time_buf), "%s", s_time_str);
    freq_valid = s_freq_valid;
    measured_freq = s_measured_freq_hz;
    
    /* Ensure s_face_name is valid before copying */
    if (s_face_name[0] != '\0') {
        snprintf(face_buf, sizeof(face_buf), "%s", s_face_name);
    } else {
        snprintf(face_buf, sizeof(face_buf), "IDLE");
    }

    xSemaphoreGive(s_state_mutex);

    if (!bsp_display_lock(0)) {
        return;
    }

    /* State display */
    if (s_state_label) {
        lv_label_set_text(s_state_label, collecting ? "Collecting" : "Idle");
    }
    
    /* Time display */
    if (s_time_display) {
        lv_label_set_text_fmt(s_time_display, "Time: %s", time_buf);
    }
    
    /* Acceleration axes - each on separate line (values in mm/s^2, integer) */
    int32_t ax_mm = (int32_t)(ax * 1000);
    int32_t ay_mm = (int32_t)(ay * 1000);
    int32_t az_mm = (int32_t)(az * 1000);
    if (s_accel_x_label) {
        lv_label_set_text_fmt(s_accel_x_label, "Accel X: %" PRId32 " mm/s^2", ax_mm);
    }
    if (s_accel_y_label) {
        lv_label_set_text_fmt(s_accel_y_label, "Accel Y: %" PRId32 " mm/s^2", ay_mm);
    }
    if (s_accel_z_label) {
        lv_label_set_text_fmt(s_accel_z_label, "Accel Z: %" PRId32 " mm/s^2", az_mm);
    }
    
    /* Samples and mode */
    if (s_samples_label) {
        lv_label_set_text_fmt(s_samples_label, "Samples: %" PRIu32, sample_count);
    }
    
    /* Frequency display */
    if (s_mode_label) {
        if (freq_valid) {
            int32_t freq_d1 = (int32_t)(measured_freq * 10);  /* Hz * 10 for integer display (0.1 Hz precision) */
            lv_label_set_text_fmt(s_mode_label, "Freq: %" PRId32 " Hz (Target: %d Hz)", 
                                  freq_d1, TARGET_SAMPLE_FREQ_HZ);
        } else {
            lv_label_set_text(s_mode_label, collecting ? "Mode: RUN" : "Mode: STOP");
        }
    }

    /* Face display — strip "Face: " prefix from s_face_name, then prepend it back */
    const char *face_text = s_face_name;
    if (strncmp(face_text, "Face: ", 6) == 0) {
        face_text += 6;
    }
    lv_label_set_text_fmt(s_face_label, "Face: %s", face_text);
    
    /* Acceleration magnitude display - always show during collecting */
    /* This is used for calibration verification (target: ~9.81 m/s²) */
    float mag = sqrtf(ax * ax + ay * ay + az * az);
    int32_t mag_mm = (int32_t)(mag * 1000);
    if (s_accel_mag_label) {
        lv_label_set_text_fmt(s_accel_mag_label, "Mag: %" PRId32 " mm/s^2", mag_mm);
    }
    
    /* Calibration mode specific display - show remaining time */
    /* This is already handled by s_face_name update in sampler_task */
    
    /* Stand/Stairs/Bend/Jump/Fall protocol progress display */
    if (s_stand_label_ui) {
        char status_buf[64];
        if (xSemaphoreTake(s_state_mutex, pdMS_TO_TICKS(20)) == pdTRUE) {
            if (s_fall_protocol_active) {
                snprintf(status_buf, sizeof(status_buf), "%s", s_fall_display);
            } else if (s_jump_protocol_active) {
                snprintf(status_buf, sizeof(status_buf), "%s", s_jump_display);
            } else if (s_bend_protocol_active) {
                snprintf(status_buf, sizeof(status_buf), "%s", s_bend_display);
            } else if (s_stairs_protocol_active) {
                snprintf(status_buf, sizeof(status_buf), "%s", s_stairs_display);
            } else if (s_stand_protocol_active) {
                snprintf(status_buf, sizeof(status_buf), "%s", s_stand_display);
            } else {
                snprintf(status_buf, sizeof(status_buf), "IDLE");
            }
            xSemaphoreGive(s_state_mutex);
        } else {
            snprintf(status_buf, sizeof(status_buf), "---");
        }
        lv_label_set_text(s_stand_label_ui, status_buf);
    }

    /* Status bar at bottom */
    const char *freq_status;
    if (freq_valid) {
        int32_t freq_diff_d2 = abs((int32_t)((measured_freq - TARGET_SAMPLE_FREQ_HZ) * 100));
        freq_status = (freq_diff_d2 < 500) ? "100Hz:OK" : "100Hz:ERR";  /* ±5.00 Hz * 100 */
    } else {
        freq_status = "100Hz:--";
    }
    lv_label_set_text_fmt(s_status_bar, "A:Stop  B(2s):Mode  SD:%s  %s",
                          sd_ready ? "OK" : "---",
                          freq_status);

    bsp_display_unlock();
}

/* ================================================================
 *  LVGL UI
 * ================================================================ */
static void create_ui(void)
{
    s_ui_ready = false;  /* Reset in case this is called again */
    bsp_display_start();
    ESP_ERROR_CHECK(bsp_display_backlight_on());

    /* 0 = block indefinitely until lock is acquired */
    if (!bsp_display_lock(0)) {
        ESP_LOGE(TAG, "Failed to lock display for UI creation");
        return;
    }

    lv_obj_t *scr = lv_disp_get_scr_act(NULL);
    lv_obj_set_style_bg_color(scr, lv_color_hex(0x101820), 0);
    lv_obj_set_style_bg_opa(scr, LV_OPA_COVER, 0);
    lv_obj_set_style_text_color(scr, lv_color_hex(0xE6E6E6), 0);

    /* Title */
    s_title_label = lv_label_create(scr);
    lv_label_set_text(s_title_label, "IMU Time Logger");
    lv_obj_set_style_text_color(s_title_label, lv_color_hex(0x6EE7FF), 0);
    lv_obj_align(s_title_label, LV_ALIGN_TOP_LEFT, 10, 10);

    /* State */
    s_state_label = lv_label_create(scr);
    lv_obj_align(s_state_label, LV_ALIGN_TOP_LEFT, 10, 40);

    /* Time */
    s_time_display = lv_label_create(scr);
    lv_obj_align(s_time_display, LV_ALIGN_TOP_LEFT, 10, 65);

    /* Acceleration axes - each on separate line */
    s_accel_x_label = lv_label_create(scr);
    lv_label_set_text(s_accel_x_label, "Accel X: 0.000 m/s²");
    lv_obj_align(s_accel_x_label, LV_ALIGN_TOP_LEFT, 10, 100);

    s_accel_y_label = lv_label_create(scr);
    lv_label_set_text(s_accel_y_label, "Accel Y: 0.000 m/s²");
    lv_obj_align(s_accel_y_label, LV_ALIGN_TOP_LEFT, 10, 125);

    s_accel_z_label = lv_label_create(scr);
    lv_label_set_text(s_accel_z_label, "Accel Z: 0.000 m/s²");
    lv_obj_align(s_accel_z_label, LV_ALIGN_TOP_LEFT, 10, 150);

    /* Samples and mode */
    s_samples_label = lv_label_create(scr);
    lv_obj_align(s_samples_label, LV_ALIGN_TOP_LEFT, 10, 185);

    s_mode_label = lv_label_create(scr);
    lv_obj_align(s_mode_label, LV_ALIGN_TOP_LEFT, 140, 185);

    /* Face display for calibration */
    s_face_label = lv_label_create(scr);
    lv_label_set_text(s_face_label, "Face: IDLE");
    lv_obj_align(s_face_label, LV_ALIGN_TOP_LEFT, 10, 210);

    /* Acceleration magnitude display for calibration verification */
    s_accel_mag_label = lv_label_create(scr);
    lv_label_set_text(s_accel_mag_label, "Mag: --.- m/s^2");
    lv_obj_align(s_accel_mag_label, LV_ALIGN_TOP_LEFT, 140, 210);

    /* Stand/Stairs protocol progress display */
    s_stand_label_ui = lv_label_create(scr);
    lv_label_set_text(s_stand_label_ui, "Mode: IDLE");
    lv_obj_set_style_text_color(s_stand_label_ui, lv_color_hex(0xFFD700), 0);
    lv_obj_align(s_stand_label_ui, LV_ALIGN_TOP_LEFT, 10, 230);

    /* Status bar at bottom */
    s_status_bar = lv_label_create(scr);
    lv_obj_align(s_status_bar, LV_ALIGN_TOP_LEFT, 10, 255);

    bsp_display_unlock();

    s_ui_ready = true;
}

/* ================================================================
 *  Wi-Fi + SNTP (from tcp_server)
 * ================================================================ */
static void wifi_event_handler(void *arg, esp_event_base_t event_base,
                                int32_t event_id, void *event_data)
{
    if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_DISCONNECTED) {
        /* Update WiFi status immediately so UI reflects disconnect */
        if (xSemaphoreTake(s_state_mutex, pdMS_TO_TICKS(100)) == pdTRUE) {
            s_wifi_connected = false;
            set_status_locked("WiFi disconnected");
            xSemaphoreGive(s_state_mutex);
        }
        /* Do NOT call refresh_ui() here — this runs in the esp_event task (stack ~4KB),
         * while lv_label_set_text_fmt() needs much more stack. The sampler_task
         * (runs every 10ms) will pick up the state change automatically. */
        if (s_retry_num < EXAMPLE_ESP_MAXIMUM_RETRY) {
            esp_wifi_connect();
            s_retry_num++;
            ESP_LOGI(TAG, "Retrying WiFi connection... (%d/%d)", s_retry_num, EXAMPLE_ESP_MAXIMUM_RETRY);
        } else {
            xEventGroupSetBits(s_wifi_event_group, WIFI_FAIL_BIT);
        }
        ESP_LOGI(TAG, "WiFi disconnected");
    } else if (event_base == IP_EVENT && event_id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *event = (ip_event_got_ip_t *)event_data;
        ESP_LOGI(TAG, "Got IP: " IPSTR, IP2STR(&event->ip_info.ip));
        s_retry_num = 0;
        xEventGroupSetBits(s_wifi_event_group, WIFI_CONNECTED_BIT);
    }
}

static void wifi_init_sta(void)
{
    s_wifi_event_group = xEventGroupCreate();
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));

    ESP_ERROR_CHECK(esp_event_handler_instance_register(WIFI_EVENT,
                                                        ESP_EVENT_ANY_ID,
                                                        &wifi_event_handler,
                                                        NULL, NULL));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(IP_EVENT,
                                                        IP_EVENT_STA_GOT_IP,
                                                        &wifi_event_handler,
                                                        NULL, NULL));

    wifi_config_t wifi_config = {
        .sta = {
            .ssid = WIFI_SSID,
            .password = WIFI_PASSWORD,
            .threshold.authmode = WIFI_AUTH_WPA2_PSK,
        },
    };
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_config));
    ESP_ERROR_CHECK(esp_wifi_start());

    ESP_LOGI(TAG, "Connecting to WiFi: %s", WIFI_SSID);

    EventBits_t bits = xEventGroupWaitBits(s_wifi_event_group,
            WIFI_CONNECTED_BIT | WIFI_FAIL_BIT,
            pdFALSE, pdFALSE, portMAX_DELAY);

    if (bits & WIFI_CONNECTED_BIT) {
        ESP_LOGI(TAG, "WiFi connected: %s", WIFI_SSID);
        if (xSemaphoreTake(s_state_mutex, pdMS_TO_TICKS(200)) == pdTRUE) {
            s_wifi_connected = true;
            set_status_locked("WiFi connected");
            xSemaphoreGive(s_state_mutex);
        }
    } else if (bits & WIFI_FAIL_BIT) {
        ESP_LOGE(TAG, "WiFi failed: %s", WIFI_SSID);
    }

    refresh_ui();
}

static void time_sync_notification_cb(struct timeval *tv)
{
    ESP_LOGI(TAG, "SNTP time synchronized!");
}

static void initialize_sntp(void)
{
    ESP_LOGI(TAG, "Initializing SNTP...");

    if (xSemaphoreTake(s_state_mutex, pdMS_TO_TICKS(200)) == pdTRUE) {
        set_status_locked("Syncing time via SNTP...");
        xSemaphoreGive(s_state_mutex);
    }
    refresh_ui();

    esp_sntp_config_t config = ESP_NETIF_SNTP_DEFAULT_CONFIG("pool.ntp.org");
    config.sync_cb = time_sync_notification_cb;
    esp_netif_sntp_init(&config);
    esp_netif_sntp_start();

    time_t now = 0;
    struct tm timeinfo = {0};
    int retry = 0;
    const int retry_count = 30;
    while (esp_netif_sntp_sync_wait(2000 / portTICK_PERIOD_MS) == ESP_ERR_TIMEOUT && ++retry < retry_count) {
        ESP_LOGI(TAG, "Waiting for time sync... (%d/%d)", retry, retry_count);
        /* Update UI periodically so user sees progress */
        if (xSemaphoreTake(s_state_mutex, pdMS_TO_TICKS(100)) == pdTRUE) {
            set_status_locked("SNTP waiting... (%d/%d)", retry, retry_count);
            xSemaphoreGive(s_state_mutex);
        }
        refresh_ui();
    }

    /* Set timezone to Beijing (UTC+8) */
    setenv("TZ", "CST-8", 1);
    tzset();

    time(&now);
    localtime_r(&now, &timeinfo);
    char strftime_buf[64];
    strftime(strftime_buf, sizeof(strftime_buf), "%Y-%m-%d %H:%M:%S", &timeinfo);
    ESP_LOGI(TAG, "Current time: %s", strftime_buf);

    if (xSemaphoreTake(s_state_mutex, pdMS_TO_TICKS(200)) == pdTRUE) {
        if (retry < retry_count) {
            /* SNTP succeeded */
            s_time_synced = true;
            snprintf(s_time_str, sizeof(s_time_str), "%s", strftime_buf);
            set_status_locked("Time synced");
        } else {
            /* SNTP failed after all retries */
            s_time_synced = false;
            snprintf(s_time_str, sizeof(s_time_str), "SNTP FAILED");
            set_status_locked("SNTP timeout - time may be wrong");
            ESP_LOGW(TAG, "SNTP time sync failed after %d retries", retry_count);
        }
        xSemaphoreGive(s_state_mutex);
    }
}

/* ================================================================
 *  QMA6100P Accelerometer (from display_rotation)
 * ================================================================ */
/* I2C pins from BSP config */
#define ACCEL_I2C_SDA             BSP_I2C_SDA    /* GPIO_NUM_4 */
#define ACCEL_I2C_SCL             BSP_I2C_SCL    /* GPIO_NUM_5 */
#define ACCEL_I2C_FREQ_HZ         400000

static esp_err_t app_accel_init(void)
{
    /* IMPORTANT: The qma6100p library uses the LEGACY I2C driver (#include "driver/i2c.h"),
     * NOT the new I2C master driver (i2c_master_bus / i2c_new_master_bus).
     * BSP's bsp_i2c_init() initializes the new driver, which is incompatible.
     * Therefore we must install the legacy I2C driver on the same port manually. */
    i2c_config_t i2c_conf = {
        .mode = I2C_MODE_MASTER,
        .sda_io_num = ACCEL_I2C_SDA,
        .scl_io_num = ACCEL_I2C_SCL,
        .sda_pullup_en = GPIO_PULLUP_ENABLE,
        .scl_pullup_en = GPIO_PULLUP_ENABLE,
        .master.clk_speed = ACCEL_I2C_FREQ_HZ,
    };
    esp_err_t ret = i2c_param_config(QMA6100P_I2C_PORT, &i2c_conf);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "i2c_param_config failed: %s", esp_err_to_name(ret));
        return ret;
    }
    ret = i2c_driver_install(QMA6100P_I2C_PORT, I2C_MODE_MASTER, 0, 0, 0);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "i2c_driver_install failed: %s", esp_err_to_name(ret));
        return ret;
    }

    const uint8_t probe_addrs[] = {QMA6100P_I2C_ADDRESS, QMA6100P_I2C_ADDRESS_1};

    for (size_t i = 0; i < sizeof(probe_addrs); i++) {
        qma6100p_handle_t probe = qma6100p_create(QMA6100P_I2C_PORT, probe_addrs[i]);
        if (probe == NULL) {
            continue;
        }

        uint8_t device_id = 0;
        esp_err_t ret = qma6100p_get_deviceid(probe, &device_id);
        if (ret == ESP_OK) {
            ESP_LOGI(TAG, "Detected QMA6100P at 0x%02X, ID 0x%02X",
                     probe_addrs[i], device_id);
            ret = qma6100p_wake_up(probe);
            if (ret == ESP_OK) {
                ret = qma6100p_config(probe, ACCE_FS_2G);
            }
            if (ret == ESP_OK) {
                s_accel = probe;
                return ESP_OK;
            }
        }
        qma6100p_delete(probe);
    }

    ESP_LOGW(TAG, "No QMA6100P accelerometer found");
    return ESP_ERR_NOT_FOUND;
}

static esp_err_t app_accel_read(float *out_x, float *out_y, float *out_z)
{
    if (out_x == NULL || out_y == NULL || out_z == NULL || s_accel == NULL) {
        return ESP_ERR_INVALID_ARG;
    }

    qma6100p_acce_value_t acce = {0};
    esp_err_t ret = qma6100p_get_acce(s_accel, &acce);
    if (ret != ESP_OK) {
        return ret;
    }

    /* Simple low-pass filter (state reset when starting a new capture session) */
    if (!s_filter_init) {
        s_filter_x = acce.acce_x;
        s_filter_y = acce.acce_y;
        s_filter_z = acce.acce_z;
        s_filter_init = true;
    } else {
        s_filter_x += ACCEL_FILTER_ALPHA * (acce.acce_x - s_filter_x);
        s_filter_y += ACCEL_FILTER_ALPHA * (acce.acce_y - s_filter_y);
        s_filter_z += ACCEL_FILTER_ALPHA * (acce.acce_z - s_filter_z);
    }

    *out_x = s_filter_x;
    *out_y = s_filter_y;
    *out_z = s_filter_z;
    return ESP_OK;
}

/* ================================================================
 *  6-Face Calibration Mode
 * ================================================================ */

/* Get face name */
static const char* get_face_name(calibration_face_t face)
{
    switch (face) {
        case FACE_POS_X: return "+X";
        case FACE_NEG_X: return "-X";
        case FACE_POS_Y: return "+Y";
        case FACE_NEG_Y: return "-Y";
        case FACE_POS_Z: return "+Z";
        case FACE_NEG_Z: return "-Z";
        default: return "IDLE";
    }
}

/* Get next face in sequence */
static calibration_face_t get_next_face(calibration_face_t current)
{
    switch (current) {
        case FACE_POS_X: return FACE_NEG_X;
        case FACE_NEG_X: return FACE_POS_Y;
        case FACE_POS_Y: return FACE_NEG_Y;
        case FACE_NEG_Y: return FACE_POS_Z;
        case FACE_POS_Z: return FACE_NEG_Z;
        case FACE_NEG_Z: return FACE_COMPLETE;
        default: return FACE_POS_X;
    }
}

/* Check if sensor is stationary (low variance in acceleration) */
static bool check_stationary(float ax, float ay, float az)
{
    s_stationary_buffer_x[s_stationary_index] = ax;
    s_stationary_buffer_y[s_stationary_index] = ay;
    s_stationary_buffer_z[s_stationary_index] = az;
    s_stationary_index = (s_stationary_index + 1) % STATIONARY_SAMPLES;

    if (!s_stationary_ready) {
        if (s_stationary_index == 0) {
            s_stationary_ready = true;
        } else {
            return false;
        }
    }

    /* Calculate variance for each axis */
    float sum_x = 0, sum_y = 0, sum_z = 0;
    float sum_sq_x = 0, sum_sq_y = 0, sum_sq_z = 0;

    for (uint32_t i = 0; i < STATIONARY_SAMPLES; i++) {
        sum_x += s_stationary_buffer_x[i];
        sum_y += s_stationary_buffer_y[i];
        sum_z += s_stationary_buffer_z[i];
        sum_sq_x += s_stationary_buffer_x[i] * s_stationary_buffer_x[i];
        sum_sq_y += s_stationary_buffer_y[i] * s_stationary_buffer_y[i];
        sum_sq_z += s_stationary_buffer_z[i] * s_stationary_buffer_z[i];
    }

    float mean_x = sum_x / STATIONARY_SAMPLES;
    float mean_y = sum_y / STATIONARY_SAMPLES;
    float mean_z = sum_z / STATIONARY_SAMPLES;

    float var_x = (sum_sq_x / STATIONARY_SAMPLES) - (mean_x * mean_x);
    float var_y = (sum_sq_y / STATIONARY_SAMPLES) - (mean_y * mean_y);
    float var_z = (sum_sq_z / STATIONARY_SAMPLES) - (mean_z * mean_z);

    /* Check if all variances are below threshold */
    return (var_x < STATIONARY_THRESHOLD * STATIONARY_THRESHOLD) &&
           (var_y < STATIONARY_THRESHOLD * STATIONARY_THRESHOLD) &&
           (var_z < STATIONARY_THRESHOLD * STATIONARY_THRESHOLD);
}

/* Detect which face is currently up based on gravity direction */
static calibration_face_t detect_current_face(float ax, float ay, float az)
{
    /* Gravity is approximately 9.8 m/s² pointing downward */
    /* When a face is "up", the opposite axis points toward gravity */
    /* Example: +Z face up → Z axis points up → accel_z ≈ -9.8 m/s² */

    float threshold = 5.0f; /* m/s² - must be close to ±g */

    /* Check Z axis first (most common orientation) */
    if (az < -threshold) {
        return FACE_POS_Z;  /* +Z face up, gravity pulls -Z */
    }
    if (az > threshold) {
        return FACE_NEG_Z;  /* -Z face up, gravity pulls +Z */
    }

    /* Check X axis */
    if (ax < -threshold) {
        return FACE_POS_X;  /* +X face up, gravity pulls -X */
    }
    if (ax > threshold) {
        return FACE_NEG_X;  /* -X face up, gravity pulls +X */
    }

    /* Check Y axis */
    if (ay < -threshold) {
        return FACE_POS_Y;  /* +Y face up, gravity pulls -Y */
    }
    if (ay > threshold) {
        return FACE_NEG_Y;  /* -Y face up, gravity pulls +Y */
    }

    return FACE_IDLE; /* Not aligned with any face */
}

/* Start calibration mode */
static esp_err_t start_calibration_locked(void)
{
    if (!s_sd_ready) {
        set_status_locked("Cannot start: SD card not mounted");
        return ESP_ERR_INVALID_STATE;
    }

    if (s_collecting) {
        return ESP_OK;
    }

    /* Create calibration data file */
    uint64_t now_ms = esp_timer_get_time() / 1000;
    snprintf(s_file_path, sizeof(s_file_path),
             BSP_SD_MOUNT_POINT "/calibration_%013" PRIu64 ".csv", now_ms);

    s_data_file = fopen(s_file_path, "w");
    if (!s_data_file) {
        int err = errno;
        s_file_path[0] = '\0';
        set_status_locked("Open file failed (errno=%d)", err);
        return ESP_FAIL;
    }

    /* CSV header for calibration data */
    fprintf(s_data_file, "label,timestamp_ms,accel_x,accel_y,accel_z\n");
    fflush(s_data_file);

    /* Create calibration parameters output file */
    const char *calib_path = BSP_SD_MOUNT_POINT "/calibration.csv";
    s_calibration_file = fopen(calib_path, "w");
    if (!s_calibration_file) {
        fclose(s_data_file);
        s_data_file = NULL;
        int err = errno;
        set_status_locked("Open calibration.csv failed (errno=%d)", err);
        return ESP_FAIL;
    }
    fprintf(s_calibration_file, "axis,offset,scale,unit\n");
    fflush(s_calibration_file);

    s_latest_accel_x = 0;
    s_latest_accel_y = 0;
    s_latest_accel_z = 0;
    s_sample_count = 0;
    s_collecting = true;

    /* Clear calibration accumulators from any previous session */
    memset(s_calib_sum_x, 0, sizeof(s_calib_sum_x));
    memset(s_calib_sum_y, 0, sizeof(s_calib_sum_y));
    memset(s_calib_sum_z, 0, sizeof(s_calib_sum_z));
    memset(s_calib_count, 0, sizeof(s_calib_count));

    /* Reset calibration state */
    s_current_face = FACE_POS_X;
    s_face_sample_count = 0;
    s_face_start_time_ms = esp_timer_get_time() / 1000;
    s_stationary_index = 0;
    s_stationary_ready = false;
    snprintf(s_face_name, sizeof(s_face_name), "Face: +X (0s)");

    /* Reset frequency measurement */
    s_freq_sample_count = 0;
    s_freq_start_time_us = esp_timer_get_time();
    s_freq_measuring = true;
    s_freq_valid = false;
    s_measured_freq_hz = 0.0f;

    /* Reset low-pass filter state for a clean new session */
    s_filter_x = 0;
    s_filter_y = 0;
    s_filter_z = 0;
    s_filter_init = false;

    set_status_locked("Calibration: +X face, 10s each");

    ESP_LOGI(TAG, "Calibration started: %s", s_file_path);
    ESP_LOGI(TAG, "Sequence: +X → -X → +Y → -Y → +Z → -Z, 10s per face");

    return ESP_OK;
}

/* Complete calibration and write calibration.csv */
static void complete_calibration_locked(void)
{
    if (s_data_file) {
        fclose(s_data_file);
        s_data_file = NULL;
    }

    s_collecting = false;
    s_current_face = FACE_IDLE;
    snprintf(s_face_name, sizeof(s_face_name), "Face: COMPLETE");

    if (s_calibration_file) {
        fclose(s_calibration_file);
        s_calibration_file = NULL;
    }

    set_status_locked("Calibration complete!");
    ESP_LOGI(TAG, "Calibration complete. See calibration.csv for parameters");
}

/* ================================================================
 *  SD Card Logging (Normal Mode)
 * ================================================================ */
static void stop_collection_locked(const char *reason)
{
    /* Alias protection: s_data_file and s_jump_data_file may point to the
     * same FILE. If so, detach s_jump_data_file so we never double-close.
     * s_jump_protocol_active guard is checked inside the block. */
    if (s_jump_data_file == s_data_file && s_data_file != NULL) {
        s_jump_data_file = NULL;
    }

    if (s_data_file) {
        fclose(s_data_file);
        s_data_file = NULL;
    }

    if (s_calibration_file) {
        fclose(s_calibration_file);
        s_calibration_file = NULL;
    }

    s_collecting = false;
    s_current_face = FACE_IDLE;
    snprintf(s_face_name, sizeof(s_face_name), "Face: IDLE");

    /* Also reset stand protocol state if active */
    s_stand_protocol_active = false;
    s_stand_state = STAND_PROTOCOL_IDLE;
    s_stand_label[0] = '\0';
    snprintf(s_stand_display, sizeof(s_stand_display), "Stand: IDLE");

    /* Also reset stairs protocol state if active */
    s_stairs_protocol_active = false;
    s_stairs_state = STAIRS_PROTOCOL_IDLE;
    s_stairs_label[0] = '\0';
    snprintf(s_stairs_display, sizeof(s_stairs_display), "Stairs: IDLE");

    /* Also reset bend protocol state if active */
    s_bend_protocol_active = false;
    s_bend_state = BEND_PROTOCOL_IDLE;
    s_bend_label[0] = '\0';
    snprintf(s_bend_display, sizeof(s_bend_display), "Bend: IDLE");

    /* Also reset jump protocol state if active */
    if (s_jump_protocol_active) {
        if (s_jump_data_file) {
            fclose(s_jump_data_file);
            s_jump_data_file = NULL;
        }
        s_jump_protocol_active = false;
        s_jump_state = JUMP_PROTOCOL_IDLE;
        s_jump_label[0] = '\0';
        snprintf(s_jump_display, sizeof(s_jump_display), "Jump: IDLE");
        s_jump_file_path[0] = '\0';
    }

    /* Also reset fall protocol state if active */
    s_fall_protocol_active = false;
    s_fall_state = FALL_PROTOCOL_IDLE;
    s_fall_label[0] = '\0';
    snprintf(s_fall_display, sizeof(s_fall_display), "Fall: IDLE");

    set_status_locked("%s", reason);
}

static void reset_accel_filter(void)
{
    s_filter_x = 0;
    s_filter_y = 0;
    s_filter_z = 0;
    s_filter_init = false;
}

static esp_err_t start_collection_locked(void)
{
    if (!s_sd_ready) {
        set_status_locked("Cannot start: SD card not mounted");
        return ESP_ERR_INVALID_STATE;
    }

    if (s_collecting) {
        return ESP_OK;
    }

    /* Use unique filename based on uptime (milliseconds) for CSV */
    uint64_t now_ms = esp_timer_get_time() / 1000;
    snprintf(s_file_path, sizeof(s_file_path),
             BSP_SD_MOUNT_POINT "/DATA_%013" PRIu64 ".csv", now_ms);

    s_data_file = fopen(s_file_path, "w");
    if (!s_data_file) {
        int err = errno;
        s_file_path[0] = '\0';
        set_status_locked("Open file failed (errno=%d)", err);
        return ESP_FAIL;
    }

    /* CSV header - label first format */
    fprintf(s_data_file, "label,timestamp_ms,accel_x,accel_y,accel_z\n");
    fflush(s_data_file);

    s_latest_accel_x = 0;
    s_latest_accel_y = 0;
    s_latest_accel_z = 0;
    s_sample_count = 0;
    s_collecting = true;

    /* Reset calibration state */
    s_current_face = FACE_IDLE;
    snprintf(s_face_name, sizeof(s_face_name), "Face: IDLE");

    /* Reset frequency measurement */
    s_freq_sample_count = 0;
    s_freq_start_time_us = esp_timer_get_time();
    s_freq_measuring = true;
    s_freq_valid = false;
    s_measured_freq_hz = 0.0f;

    /* Reset low-pass filter state for a clean new session */
    reset_accel_filter();
    set_status_locked("Logging IMU data @ 100 Hz");

    return ESP_OK;
}

/* ================================================================
 *  Stand Action Protocol
 * ================================================================ */

/* Create stand file for current group */
static esp_err_t create_stand_file_for_current_group(void)
{
    /* Close previous file if open */
    if (s_stand_data_file) {
        fclose(s_stand_data_file);
        s_stand_data_file = NULL;
        s_stand_file_open = false;
    }

    /* Create stand data file for current group */
    char stand_file_path[STAND_FILE_PATH_LEN];
    int group_num = s_stand_group + 1;  /* 1-based */
    snprintf(stand_file_path, sizeof(stand_file_path),
             BSP_SD_MOUNT_POINT "/stand_%d.csv", group_num);

    s_stand_data_file = fopen(stand_file_path, "w");
    if (!s_stand_data_file) {
        int err = errno;
        set_status_locked("Open file failed (errno=%d)", err);
        return ESP_FAIL;
    }
    s_stand_file_open = true;

    /* CSV header - label first format */
    fprintf(s_stand_data_file, "label,timestamp_ms,accel_x,accel_y,accel_z\n");
    fflush(s_stand_data_file);

    /* Sync data_file to stand_data_file so sampler_task can write */
    s_data_file = s_stand_data_file;

    ESP_LOGI(TAG, "[Stand] Created file: %s", stand_file_path);

    return ESP_OK;
}

/* Start stand protocol (called from Button B long press) */
static esp_err_t start_stand_protocol_locked(void)
{
    if (!s_sd_ready) {
        set_status_locked("Cannot start: SD card not mounted");
        return ESP_ERR_INVALID_STATE;
    }

    if (s_collecting) {
        set_status_locked("Stop current session first");
        return ESP_ERR_INVALID_STATE;
    }

    s_latest_accel_x = 0;
    s_latest_accel_y = 0;
    s_latest_accel_z = 0;
    s_sample_count = 0;
    s_collecting = true;

    /* Reset calibration state (not used in stand protocol) */
    s_current_face = FACE_IDLE;
    snprintf(s_face_name, sizeof(s_face_name), "Face: IDLE");

    /* Initialize stand protocol state */
    s_stand_protocol_active = true;
    s_stand_state = STAND_PROTOCOL_PREP;
    s_stand_group = 0;
    s_stand_phase_start_ms = esp_timer_get_time() / 1000;
    s_stand_duration_sec = STAND_EXECUTE_DURATION_MIN_SEC + 
        (esp_random() % (STAND_EXECUTE_DURATION_MAX_SEC - STAND_EXECUTE_DURATION_MIN_SEC + 1));
    snprintf(s_stand_label, sizeof(s_stand_label), "stand");
    snprintf(s_stand_display, sizeof(s_stand_display), "Stand: Prep G1 (3s)");

    /* Reset frequency measurement */
    s_freq_sample_count = 0;
    s_freq_start_time_us = esp_timer_get_time();
    s_freq_measuring = true;
    s_freq_valid = false;
    s_measured_freq_hz = 0.0f;

    /* Reset low-pass filter */
    reset_accel_filter();

    /* Create file for group 1 */
    esp_err_t ret = create_stand_file_for_current_group();
    if (ret != ESP_OK) {
        return ret;
    }

    set_status_locked("Stand: Group 1 prep (3s)");

    ESP_LOGI(TAG, "Stand protocol started: 3 groups, each in separate file");
    ESP_LOGI(TAG, "Protocol: 3x(prep 3s + stand 20-30s + idle 5s)");

    return ESP_OK;
}

/* Complete stand protocol and close file */
static void complete_stand_protocol_locked(void)
{
    if (s_data_file) {
        fclose(s_data_file);
        s_data_file = NULL;
    }

    s_collecting = false;
    s_stand_protocol_active = false;
    s_stand_state = STAND_PROTOCOL_COMPLETE;
    s_stand_label[0] = '\0';
    snprintf(s_stand_display, sizeof(s_stand_display), "Stand: DONE");
    s_current_face = FACE_IDLE;
    snprintf(s_face_name, sizeof(s_face_name), "Face: IDLE");

    set_status_locked("Stand protocol complete!");
    ESP_LOGI(TAG, "Stand protocol complete. %" PRIu32 " samples saved", s_sample_count);
}

/* Start stairs protocol (called from Button B long press) */
static esp_err_t start_stairs_protocol_locked(void)
{
    if (!s_sd_ready) {
        set_status_locked("Cannot start: SD card not mounted");
        return ESP_ERR_INVALID_STATE;
    }

    if (s_collecting) {
        set_status_locked("Stop current session first");
        return ESP_ERR_INVALID_STATE;
    }

    /* Create stairs data file for group 1 */
    snprintf(s_file_path, sizeof(s_file_path),
             BSP_SD_MOUNT_POINT "/stairs_1.csv");

    s_data_file = fopen(s_file_path, "w");
    if (!s_data_file) {
        int err = errno;
        s_file_path[0] = '\0';
        set_status_locked("Open file failed (errno=%d)", err);
        return ESP_FAIL;
    }

    /* CSV header - label first format */
    fprintf(s_data_file, "label,timestamp_ms,accel_x,accel_y,accel_z\n");
    fflush(s_data_file);

    s_latest_accel_x = 0;
    s_latest_accel_y = 0;
    s_latest_accel_z = 0;
    s_sample_count = 0;
    s_collecting = true;

    /* Reset calibration state (not used in stairs protocol) */
    s_current_face = FACE_IDLE;
    snprintf(s_face_name, sizeof(s_face_name), "Face: IDLE");

    /* Initialize stairs protocol state */
    s_stairs_protocol_active = true;
    s_stairs_state = STAIRS_PROTOCOL_PREP;
    s_stairs_group = 0;
    s_stairs_phase_start_ms = esp_timer_get_time() / 1000;
    s_stairs_duration_sec = STAIRS_EXECUTE_DURATION_MIN_SEC + 
        (esp_random() % (STAIRS_EXECUTE_DURATION_MAX_SEC - STAIRS_EXECUTE_DURATION_MIN_SEC + 1));
    snprintf(s_stairs_label, sizeof(s_stairs_label), "stairs");
    snprintf(s_stairs_display, sizeof(s_stairs_display), "Stairs: Prep G1 (3s)");

    /* Reset frequency measurement */
    s_freq_sample_count = 0;
    s_freq_start_time_us = esp_timer_get_time();
    s_freq_measuring = true;
    s_freq_valid = false;
    s_measured_freq_hz = 0.0f;

    /* Reset low-pass filter */
    reset_accel_filter();

    set_status_locked("Stairs: Group 1 (25s)");

    ESP_LOGI(TAG, "Stairs protocol started: %s", s_file_path);
    ESP_LOGI(TAG, "Protocol: 3x(stairs 20-30s), continuous groups");

    return ESP_OK;
}

/* Create stairs file for current group */
static esp_err_t create_stairs_file_for_current_group(void)
{
    /* Close previous file if open */
    if (s_data_file) {
        fclose(s_data_file);
        s_data_file = NULL;
    }

    /* Create stairs data file for current group */
    char stairs_file_path[STAIRS_FILE_PATH_LEN];
    int group_num = s_stairs_group + 1;  /* 1-based */
    snprintf(stairs_file_path, sizeof(stairs_file_path),
             BSP_SD_MOUNT_POINT "/stairs_%d.csv", group_num);

    s_data_file = fopen(stairs_file_path, "w");
    if (!s_data_file) {
        int err = errno;
        set_status_locked("Open file failed (errno=%d)", err);
        return ESP_FAIL;
    }

    /* CSV header - label first format */
    fprintf(s_data_file, "label,timestamp_ms,accel_x,accel_y,accel_z\n");
    fflush(s_data_file);

    ESP_LOGI(TAG, "[Stairs] Created file: %s", stairs_file_path);

    return ESP_OK;
}

/* Complete stairs protocol and close file */
static void complete_stairs_protocol_locked(void)
{
    if (s_data_file) {
        fclose(s_data_file);
        s_data_file = NULL;
    }

    s_collecting = false;
    s_stairs_protocol_active = false;
    s_stairs_state = STAIRS_PROTOCOL_COMPLETE;
    s_stairs_label[0] = '\0';
    snprintf(s_stairs_display, sizeof(s_stairs_display), "Stairs: DONE");
    s_current_face = FACE_IDLE;
    snprintf(s_face_name, sizeof(s_face_name), "Face: IDLE");

    set_status_locked("Stairs protocol complete!");
    ESP_LOGI(TAG, "Stairs protocol complete. %" PRIu32 " samples saved", s_sample_count);
}

/* Start bend protocol (called from Button B long press) */
static esp_err_t start_bend_protocol_locked(void)
{
    if (!s_sd_ready) {
        set_status_locked("Cannot start: SD card not mounted");
        return ESP_ERR_INVALID_STATE;
    }

    if (s_collecting) {
        set_status_locked("Stop current session first");
        return ESP_ERR_INVALID_STATE;
    }

    /* Create bend data file for group 1 */
    snprintf(s_file_path, sizeof(s_file_path),
             BSP_SD_MOUNT_POINT "/bend_1.csv");

    s_data_file = fopen(s_file_path, "w");
    if (!s_data_file) {
        int err = errno;
        s_file_path[0] = '\0';
        set_status_locked("Open file failed (errno=%d)", err);
        return ESP_FAIL;
    }

    /* CSV header - label first format */
    fprintf(s_data_file, "label,timestamp_ms,accel_x,accel_y,accel_z\n");
    fflush(s_data_file);

    s_latest_accel_x = 0;
    s_latest_accel_y = 0;
    s_latest_accel_z = 0;
    s_sample_count = 0;
    s_collecting = true;

    /* Reset calibration state (not used in bend protocol) */
    s_current_face = FACE_IDLE;
    snprintf(s_face_name, sizeof(s_face_name), "Face: IDLE");

    /* Initialize bend protocol state */
    s_bend_protocol_active = true;
    s_bend_state = BEND_PROTOCOL_PREP;
    s_bend_group = 0;
    s_bend_phase_start_ms = esp_timer_get_time() / 1000;
    s_bend_duration_sec = BEND_DURATION_MIN_SEC;  /* Start with 20s */
    s_bend_interval_duration_sec = BEND_INTERVAL_MIN_SEC;  /* Start with 60s */
    snprintf(s_bend_label, sizeof(s_bend_label), "bend");
    snprintf(s_bend_display, sizeof(s_bend_display), "Bend: Prep G1 (3s)");

    /* Reset frequency measurement */
    s_freq_sample_count = 0;
    s_freq_start_time_us = esp_timer_get_time();
    s_freq_measuring = true;
    s_freq_valid = false;
    s_measured_freq_hz = 0.0f;

    /* Reset low-pass filter */
    reset_accel_filter();

    set_status_locked("Bend: Group 1 (%ds)", s_bend_duration_sec);

    ESP_LOGI(TAG, "Bend protocol started: %s", s_file_path);
    ESP_LOGI(TAG, "Protocol: 3x(bend 20-30s + rest 60-90s)");

    return ESP_OK;
}

/* Create bend file for current group */
static esp_err_t create_bend_file_for_current_group(void)
{
    /* Close previous file if open */
    if (s_data_file) {
        fclose(s_data_file);
        s_data_file = NULL;
    }

    /* Create bend data file for current group */
    char bend_file_path[FILE_PATH_LEN];
    int group_num = s_bend_group + 1;  /* 1-based */
    snprintf(bend_file_path, sizeof(bend_file_path),
             BSP_SD_MOUNT_POINT "/bend_%d.csv", group_num);

    s_data_file = fopen(bend_file_path, "w");
    if (!s_data_file) {
        int err = errno;
        set_status_locked("Open file failed (errno=%d)", err);
        return ESP_FAIL;
    }

    /* CSV header - label first format */
    fprintf(s_data_file, "label,timestamp_ms,accel_x,accel_y,accel_z\n");
    fflush(s_data_file);

    ESP_LOGI(TAG, "[Bend] Created file: %s", bend_file_path);

    return ESP_OK;
}

/* Complete bend protocol and close file */
static void complete_bend_protocol_locked(void)
{
    if (s_data_file) {
        fclose(s_data_file);
        s_data_file = NULL;
    }

    s_collecting = false;
    s_bend_protocol_active = false;
    s_bend_state = BEND_PROTOCOL_COMPLETE;
    s_bend_label[0] = '\0';
    snprintf(s_bend_display, sizeof(s_bend_display), "Bend: DONE");
    s_current_face = FACE_IDLE;
    snprintf(s_face_name, sizeof(s_face_name), "Face: IDLE");

    set_status_locked("Bend protocol complete!");
    ESP_LOGI(TAG, "Bend protocol complete. %" PRIu32 " samples saved", s_sample_count);
}

/* Start jump protocol (called from Button B long press) */
static esp_err_t start_jump_protocol_locked(void)
{
    if (!s_sd_ready) {
        set_status_locked("Cannot start: SD card not mounted");
        return ESP_ERR_INVALID_STATE;
    }

    if (s_collecting) {
        set_status_locked("Stop current session first");
        return ESP_ERR_INVALID_STATE;
    }

    /* Create jump data file for group 1 */
    snprintf(s_jump_file_path, sizeof(s_jump_file_path),
             BSP_SD_MOUNT_POINT "/jump_1.csv");

    s_jump_data_file = fopen(s_jump_file_path, "w");
    if (!s_jump_data_file) {
        int err = errno;
        s_jump_file_path[0] = '\0';
        set_status_locked("Open file failed (errno=%d)", err);
        return ESP_FAIL;
    }

    /* CSV header - label first format */
    fprintf(s_jump_data_file, "label,timestamp_ms,accel_x,accel_y,accel_z\n");
    fflush(s_jump_data_file);

    s_latest_accel_x = 0;
    s_latest_accel_y = 0;
    s_latest_accel_z = 0;
    s_sample_count = 0;
    s_collecting = true;

    /* Reset calibration state (not used in jump protocol) */
    s_current_face = FACE_IDLE;
    snprintf(s_face_name, sizeof(s_face_name), "Face: IDLE");

    /* Initialize jump protocol state */
    s_jump_protocol_active = true;
    s_jump_state = JUMP_PROTOCOL_PREP;
    s_jump_group = 0;
    s_jump_phase_start_ms = esp_timer_get_time() / 1000;
    s_jump_duration_sec = JUMP_EXECUTE_DURATION_MIN_SEC + 
        (esp_random() % (JUMP_EXECUTE_DURATION_MAX_SEC - JUMP_EXECUTE_DURATION_MIN_SEC + 1));
    snprintf(s_jump_label, sizeof(s_jump_label), "jump");
    snprintf(s_jump_display, sizeof(s_jump_display), "Jump: Prep G1 (3s)");

    /* Reset frequency measurement */
    s_freq_sample_count = 0;
    s_freq_start_time_us = esp_timer_get_time();
    s_freq_measuring = true;
    s_freq_valid = false;
    s_measured_freq_hz = 0.0f;

    /* Reset low-pass filter */
    reset_accel_filter();

    set_status_locked("Jump: Group 1 (10s)");

    /* Sync data_file to jump_data_file so sampler_task can write */
    s_data_file = s_jump_data_file;

    ESP_LOGI(TAG, "Jump protocol started: %s", s_jump_file_path);
    ESP_LOGI(TAG, "Protocol: 3x(jump 5-10s + rest 5-10s)");

    return ESP_OK;
}

/* Complete jump protocol and close all files */
static void complete_jump_protocol_locked(void)
{
    if (s_jump_data_file) {
        fclose(s_jump_data_file);
        s_jump_data_file = NULL;
    }

    s_collecting = false;
    s_jump_protocol_active = false;
    s_jump_state = JUMP_PROTOCOL_COMPLETE;
    s_jump_label[0] = '\0';
    snprintf(s_jump_display, sizeof(s_jump_display), "Jump: DONE");
    s_current_face = FACE_IDLE;
    snprintf(s_face_name, sizeof(s_face_name), "Face: IDLE");

    set_status_locked("Jump protocol complete!");
    ESP_LOGI(TAG, "Jump protocol complete. %" PRIu32 " samples saved", s_sample_count);
}

/* Start fall protocol (called from Button B long press) */
static esp_err_t start_fall_protocol_locked(void)
{
    if (!s_sd_ready) {
        set_status_locked("Cannot start: SD card not mounted");
        return ESP_ERR_INVALID_STATE;
    }

    if (s_collecting) {
        set_status_locked("Stop current session first");
        return ESP_ERR_INVALID_STATE;
    }

    /* Create fall data file for group 1 */
    snprintf(s_file_path, sizeof(s_file_path),
             BSP_SD_MOUNT_POINT "/fall_1.csv");

    s_data_file = fopen(s_file_path, "w");
    if (!s_data_file) {
        int err = errno;
        s_file_path[0] = '\0';
        set_status_locked("Open file failed (errno=%d)", err);
        return ESP_FAIL;
    }

    /* CSV header - label first format */
    fprintf(s_data_file, "label,timestamp_ms,accel_x,accel_y,accel_z\n");
    fflush(s_data_file);

    s_latest_accel_x = 0;
    s_latest_accel_y = 0;
    s_latest_accel_z = 0;
    s_sample_count = 0;
    s_collecting = true;

    /* Reset calibration state (not used in fall protocol) */
    s_current_face = FACE_IDLE;
    snprintf(s_face_name, sizeof(s_face_name), "Face: IDLE");

    /* Initialize fall protocol state */
    s_fall_protocol_active = true;
    s_fall_state = FALL_PROTOCOL_PREP;
    s_fall_group = 0;
    s_fall_phase_start_ms = esp_timer_get_time() / 1000;
    s_fall_duration_sec = FALL_EXECUTE_DURATION_MIN_SEC + 
        (esp_random() % (FALL_EXECUTE_DURATION_MAX_SEC - FALL_EXECUTE_DURATION_MIN_SEC + 1));
    s_fall_interval_duration_sec = FALL_INTERVAL_MIN_SEC + 
        (esp_random() % (FALL_INTERVAL_MAX_SEC - FALL_INTERVAL_MIN_SEC + 1));
    snprintf(s_fall_label, sizeof(s_fall_label), "fall");
    snprintf(s_fall_display, sizeof(s_fall_display), "Fall: Prep G1 (3s)");

    /* Reset frequency measurement */
    s_freq_sample_count = 0;
    s_freq_start_time_us = esp_timer_get_time();
    s_freq_measuring = true;
    s_freq_valid = false;
    s_measured_freq_hz = 0.0f;

    /* Reset low-pass filter */
    reset_accel_filter();

    set_status_locked("Fall: Group 1 (%ds)", s_fall_duration_sec);

    ESP_LOGI(TAG, "Fall protocol started: %s", s_file_path);
    ESP_LOGI(TAG, "Protocol: 3x(fall 5-10s + rest 15-30s)");

    return ESP_OK;
}

/* Complete fall protocol and close file */
static void complete_fall_protocol_locked(void)
{
    if (s_data_file) {
        fclose(s_data_file);
        s_data_file = NULL;
    }

    s_collecting = false;
    s_fall_protocol_active = false;
    s_fall_state = FALL_PROTOCOL_COMPLETE;
    s_fall_label[0] = '\0';
    snprintf(s_fall_display, sizeof(s_fall_display), "Fall: DONE");
    s_current_face = FACE_IDLE;
    snprintf(s_face_name, sizeof(s_face_name), "Face: IDLE");

    set_status_locked("Fall protocol complete!");
    ESP_LOGI(TAG, "Fall protocol complete. %" PRIu32 " samples saved", s_sample_count);
}

/* Button B single click callback — start/stop 6-face calibration */
static void button_b_single_click_cb(void *arg, void *data)
{
    (void)arg;
    (void)data;

    if (xSemaphoreTake(s_state_mutex, pdMS_TO_TICKS(200)) != pdTRUE) {
        return;
    }

    /* If any protocol or collection is running, stop it first */
    if (s_stand_protocol_active || s_stairs_protocol_active || 
        s_bend_protocol_active || s_jump_protocol_active || 
        s_fall_protocol_active || s_collecting) {
        /* If already in calibration mode, complete it */
        if (s_current_face != FACE_IDLE && s_current_face != FACE_COMPLETE) {
            ESP_LOGI(TAG, "Calibration aborted");
            stop_collection_locked("Calibration aborted by user");
        } else {
            /* Otherwise just stop the current session */
            char reason[UI_TEXT_LEN];
            snprintf(reason, sizeof(reason), "Session stopped");
            stop_collection_locked(reason);
        }
    } else {
        /* Start calibration mode */
        esp_err_t ret = start_calibration_locked();
        if (ret != ESP_OK) {
            ESP_LOGW(TAG, "Failed to start calibration: %s", esp_err_to_name(ret));
        } else {
            ESP_LOGI(TAG, "Calibration started");
        }
    }

    xSemaphoreGive(s_state_mutex);
    refresh_ui();
}

/* Button B long press callback — cycle through stand, stairs, bend, jump, and fall protocol */
static void button_b_long_press_cb(void *arg, void *data)
{
    (void)arg;
    (void)data;

    if (xSemaphoreTake(s_state_mutex, pdMS_TO_TICKS(200)) != pdTRUE) {
        return;
    }

    /* Cycle through stand, stairs, bend, jump, and fall protocols */
    esp_err_t ret;
    static int protocol_cycle = 0;
    
    /* If any protocol or collection is running, stop it first */
    if (s_stand_protocol_active || s_stairs_protocol_active || 
        s_bend_protocol_active || s_jump_protocol_active || 
        s_fall_protocol_active || s_collecting) {
        ESP_LOGI(TAG, "Current session aborted, switching protocol");
        stop_collection_locked("Protocol switch");
        vTaskDelay(pdMS_TO_TICKS(50));  /* Brief delay to ensure cleanup */
    }
    
    switch (protocol_cycle % 5) {
        case 0:
            /* Start stand protocol */
            ret = start_stand_protocol_locked();
            if (ret == ESP_OK) {
                ESP_LOGI(TAG, "Stand protocol started: %s", s_file_path);
            } else {
                ESP_LOGW(TAG, "Failed to start stand protocol: %s", esp_err_to_name(ret));
            }
            break;
        case 1:
            /* Start stairs protocol */
            ret = start_stairs_protocol_locked();
            if (ret == ESP_OK) {
                ESP_LOGI(TAG, "Stairs protocol started: %s", s_file_path);
            } else {
                ESP_LOGW(TAG, "Failed to start stairs protocol: %s", esp_err_to_name(ret));
            }
            break;
        case 2:
            /* Start bend protocol */
            ret = start_bend_protocol_locked();
            if (ret == ESP_OK) {
                ESP_LOGI(TAG, "Bend protocol started: %s", s_file_path);
            } else {
                ESP_LOGW(TAG, "Failed to start bend protocol: %s", esp_err_to_name(ret));
            }
            break;
        case 3:
            /* Start jump protocol */
            ret = start_jump_protocol_locked();
            if (ret == ESP_OK) {
                ESP_LOGI(TAG, "Jump protocol started: %s", s_jump_file_path);
            } else {
                ESP_LOGW(TAG, "Failed to start jump protocol: %s", esp_err_to_name(ret));
            }
            break;
        case 4:
            /* Start fall protocol */
            ret = start_fall_protocol_locked();
            if (ret == ESP_OK) {
                ESP_LOGI(TAG, "Fall protocol started: %s", s_file_path);
            } else {
                ESP_LOGW(TAG, "Failed to start fall protocol: %s", esp_err_to_name(ret));
            }
            break;
    }
    protocol_cycle++;

    xSemaphoreGive(s_state_mutex);
    refresh_ui();
}

/* Calculate and write calibration parameters */
static void calculate_and_write_calibration(float ax_mean, float ay_mean, float az_mean)
{
    (void)ax_mean; (void)ay_mean; (void)az_mean;
    if (!s_calibration_file) return;

    /* 
     * Calibration formula: real_value = (raw_value - offset) / scale
     * 
     * For each face, we know the expected gravity value:
     * +X face up: ax ≈ -9.80665, ay ≈ 0, az ≈ 0
     * -X face up: ax ≈ +9.80665, ay ≈ 0, az ≈ 0
     * +Y face up: ax ≈ 0, ay ≈ -9.80665, az ≈ 0
     * -Y face up: ax ≈ 0, ay ≈ +9.80665, az ≈ 0
     * +Z face up: ax ≈ 0, ay ≈ 0, az ≈ -9.80665
     * -Z face up: ax ≈ 0, ay ≈ 0, az ≈ +9.80665
     * 
     * We collect data from all 6 faces and calculate:
     * offset = (sum of raw values for + direction - sum of raw values for - direction) / 2
     * scale = gravity / ((sum of + direction raw - sum of - direction raw) / 2)
     */
}

/* ================================================================
 *  Button Callback
 * ================================================================ */
static void button_a_single_click_cb(void *arg, void *data)
{
    (void)arg;
    (void)data;

    if (xSemaphoreTake(s_state_mutex, pdMS_TO_TICKS(200)) != pdTRUE) {
        return;
    }

    if (s_collecting) {
        char reason[UI_TEXT_LEN];
        snprintf(reason, sizeof(reason), "Saved %" PRIu32 " samples", s_sample_count);
        stop_collection_locked(reason);
        
        /* Log final frequency measurement (integer: Hz*100 for 0.01 Hz precision) */
        if (s_freq_valid) {
            int32_t freq_d2 = (int32_t)(s_measured_freq_hz * 100);
            ESP_LOGI(TAG, "Session complete. Measured frequency: %" PRId32 " Hz (Target: %d Hz)", 
                     freq_d2, TARGET_SAMPLE_FREQ_HZ);
        }
        
        ESP_LOGI(TAG, "Logging stopped");
    } else {
        esp_err_t ret = start_collection_locked();
        if (ret == ESP_OK) {
            ESP_LOGI(TAG, "Logging started: %s", s_file_path);
        } else {
            ESP_LOGW(TAG, "Failed to start: %s", esp_err_to_name(ret));
        }
    }

    xSemaphoreGive(s_state_mutex);
    refresh_ui();
}

static void init_buttons(void)
{
    int button_count = 0;

    ESP_ERROR_CHECK(bsp_iot_button_create(s_buttons, &button_count, BSP_BUTTON_NUM));
    ESP_LOGI(TAG, "Initialized %d buttons, using BSP_BUTTON_1 + BSP_BUTTON_2", button_count);

    /* Button A: single click → start/stop normal collection */
    ESP_ERROR_CHECK(iot_button_register_cb(s_buttons[START_BUTTON_INDEX],
                                           BUTTON_SINGLE_CLICK,
                                           NULL,
                                           button_a_single_click_cb,
                                           NULL));

    /* Button B: single click → start/stop 6-face calibration
 *             long press (2s) → toggle stand/stairs/bend/jump/fall protocol */
    if (button_count > 1) {
        /* Single click: start/stop 6-face calibration */
        ESP_ERROR_CHECK(iot_button_register_cb(s_buttons[BSP_BUTTON_2],
                                               BUTTON_SINGLE_CLICK,
                                               NULL,
                                               button_b_single_click_cb,
                                               NULL));
        
        /* Long press (2s): cycle through action protocols */
        button_event_args_t long_press_args = {
            .long_press.press_time = 2000,  /* 2 second long press */
        };
        ESP_ERROR_CHECK(iot_button_register_cb(s_buttons[BSP_BUTTON_2],
                                               BUTTON_LONG_PRESS_START,
                                               &long_press_args,
                                               button_b_long_press_cb,
                                               NULL));
        ESP_LOGI(TAG, "Button B registered: single click = 6-face calibration, long press (2s) = toggle protocols");
    } else {
        ESP_LOGW(TAG, "Only 1 button found — Button B unavailable");
    }
}

static void init_sdcard(void)
{
    esp_err_t ret = bsp_sdcard_mount();

    if (xSemaphoreTake(s_state_mutex, pdMS_TO_TICKS(200)) != pdTRUE) {
        return;
    }

    if (ret == ESP_OK) {
        s_sd_ready = true;
        set_status_locked("SD card ready");
        ESP_LOGI(TAG, "SD mounted at %s", BSP_SD_MOUNT_POINT);
        sdmmc_card_t *card = bsp_sdcard_get_handle();
        if (card) {
            sdmmc_card_print_info(stdout, card);
        }
    } else {
        s_sd_ready = false;
        set_status_locked("SD mount failed");
        ESP_LOGE(TAG, "SD mount failed: %s", esp_err_to_name(ret));
    }

    xSemaphoreGive(s_state_mutex);
}

/* ================================================================
 *  Sampler Task — reads IMU + SNTP time → writes CSV
 * ================================================================ */
static void sampler_task(void *arg)
{
    (void)arg;

    /* Use a phase accumulator for drift-free timing.
     * next_wake_us is the absolute target time for the NEXT tick. */
    int64_t next_wake_us = esp_timer_get_time() + (SAMPLE_PERIOD_MS * 1000);

    while (true) {
        /* Do NOT refresh UI in every loop — only when state changes or periodically.
         * This reduces jitter in the critical sampling path. */

        if (xSemaphoreTake(s_state_mutex, pdMS_TO_TICKS(5)) == pdTRUE) {
            if (s_collecting && s_data_file) {
                /* 1. Read accelerometer */
                float ax, ay, az;
                esp_err_t ret = app_accel_read(&ax, &ay, &az);
                if (ret != ESP_OK) {
                    stop_collection_locked("IMU read failed");
                    xSemaphoreGive(s_state_mutex);
                    /* Resync timing after error */
                    next_wake_us = esp_timer_get_time() + (SAMPLE_PERIOD_MS * 1000);
                    continue;
                }

                /* 2. Anchor base timestamp on first sample of the session */
                if (s_sample_count == 0) {
                    struct timeval tv;
                    gettimeofday(&tv, NULL);
                    s_base_timestamp_ms = (uint64_t)tv.tv_sec * 1000 + tv.tv_usec / 1000;
                }
                
                /* Generate strictly monotonic timestamp: base + sample_index * 10ms
                 * This guarantees np.diff(timestamp_ms) == 10 for all records,
                 * independent of FreeRTOS scheduling jitter. */
                uint64_t timestamp_ms = s_base_timestamp_ms + (uint64_t)s_sample_count * SAMPLE_PERIOD_MS;

                /* 2b. Update time string for UI display (every 100 samples) */
                if (s_sample_count % 100 == 0) {
                    struct timeval tv;
                    gettimeofday(&tv, NULL);
                    struct tm timeinfo;
                    localtime_r(&tv.tv_sec, &timeinfo);
                    char strftime_buf[64];
                    strftime(strftime_buf, sizeof(strftime_buf), "%Y-%m-%d %H:%M:%S", &timeinfo);
                    snprintf(s_time_str, sizeof(s_time_str), "%s", strftime_buf);
                }

                /* 3. Convert acceleration from g to m/s² */
                float ax_ms2 = ax * GRAVITY_ACCEL;
                float ay_ms2 = ay * GRAVITY_ACCEL;
                float az_ms2 = az * GRAVITY_ACCEL;

                /* 4. Write CSV row - format: label,timestamp_ms,accel_x,accel_y,accel_z */
                char label_buf[16];
                if (s_stand_protocol_active && s_stand_label[0] != '\0') {
                    /* Stand protocol mode - use stand label */
                    snprintf(label_buf, sizeof(label_buf), "%s", s_stand_label);
                } else if (s_stairs_protocol_active && s_stairs_label[0] != '\0') {
                    /* Stairs protocol mode - use stairs label */
                    snprintf(label_buf, sizeof(label_buf), "%s", s_stairs_label);
                } else if (s_bend_protocol_active && s_bend_label[0] != '\0') {
                    /* Bend protocol mode - use bend label */
                    snprintf(label_buf, sizeof(label_buf), "%s", s_bend_label);
                } else if (s_jump_protocol_active && s_jump_label[0] != '\0') {
                    /* Jump protocol mode - use jump label */
                    snprintf(label_buf, sizeof(label_buf), "%s", s_jump_label);
                } else if (s_fall_protocol_active && s_fall_label[0] != '\0') {
                    /* Fall protocol mode - use fall label */
                    snprintf(label_buf, sizeof(label_buf), "%s", s_fall_label);
                } else if (s_current_face != FACE_IDLE && s_current_face != FACE_COMPLETE) {
                    /* Calibration mode - use face name as label */
                    snprintf(label_buf, sizeof(label_buf), "%s", s_face_name + 6);  /* Skip "Face: " prefix */
                } else {
                    /* Normal mode */
                    snprintf(label_buf, sizeof(label_buf), "%s", DATA_LABEL);
                }
                
                /* Use jump_data_file if jump protocol is active, otherwise use data_file */
                FILE *write_file = s_data_file;
                if (s_jump_protocol_active && s_jump_data_file) {
                    write_file = s_jump_data_file;
                }
                
                if (fprintf(write_file, "%s,%" PRIu64 ",%.4f,%.4f,%.4f\n",
                            label_buf,
                            timestamp_ms,
                            ax_ms2, ay_ms2, az_ms2) < 0) {
                    stop_collection_locked("Write failed");
                    ESP_LOGE(TAG, "CSV write failed");
                } else {
                    fflush(write_file);
                    /* Store raw m/s² values for UI (refresh_ui converts to mm/s² for display) */
                    s_latest_accel_x = ax_ms2;
                    s_latest_accel_y = ay_ms2;
                    s_latest_accel_z = az_ms2;
                    s_sample_count++;
                    
                    /* Accumulate calibration data if in calibration mode */
                    if (s_current_face != FACE_IDLE && s_current_face != FACE_COMPLETE) {
                        s_calib_sum_x[s_current_face] += ax_ms2;
                        s_calib_sum_y[s_current_face] += ay_ms2;
                        s_calib_sum_z[s_current_face] += az_ms2;
                        s_calib_count[s_current_face]++;
                        s_face_sample_count++;
                    }
                }

                /* 5. Calibration mode state machine */
                if (s_current_face != FACE_IDLE && s_current_face != FACE_COMPLETE) {
                    int64_t now_ms = esp_timer_get_time() / 1000;
                    int64_t elapsed_ms = now_ms - s_face_start_time_ms;

                    /* Calculate acceleration magnitude for verification */
                    float accel_mag = sqrtf(ax_ms2 * ax_ms2 + ay_ms2 * ay_ms2 + az_ms2 * az_ms2);
                    
                    /* Log magnitude for calibration verification (every 1 second) */
                    static int64_t last_mag_log_ms = 0;
                    if (elapsed_ms - last_mag_log_ms >= 1000) {
                        last_mag_log_ms = elapsed_ms;
                        const char *face_str = get_face_name(s_current_face);
                        int32_t accel_mag_mm = (int32_t)(accel_mag * 1000);
                        ESP_LOGI(TAG, "[%s] Mag: %" PRId32 " mm/s² (Target: 9810, OK: 9600-10000)", 
                                 face_str, accel_mag_mm);
                    }

                    /* Check if face duration complete */
                    if (elapsed_ms >= FACE_DURATION_MS) {
                        /* Face complete, move to next */
                        calibration_face_t prev_face = s_current_face;
                        s_current_face = get_next_face(s_current_face);
                        s_face_sample_count = 0;
                        s_face_start_time_ms = now_ms;

                        if (s_current_face == FACE_COMPLETE) {
                            /* All faces complete - calculate and write calibration */
                            const float g = GRAVITY_ACCEL;

                            /* Calculate offset and scale for each axis */
                            /* offset = (pos_sum - neg_sum) / (2 * count) */
                            /* scale = g / ((pos_sum + neg_sum) / (2 * count)) */

                            /* X axis: +X face (ax ≈ -g), -X face (ax ≈ +g) */
                            float x_offset = 0, x_scale = 1.0f;
                            if (s_calib_count[FACE_POS_X] > 0 && s_calib_count[FACE_NEG_X] > 0) {
                                float x_pos_mean = s_calib_sum_x[FACE_NEG_X] / s_calib_count[FACE_NEG_X];  /* Should be +g */
                                float x_neg_mean = s_calib_sum_x[FACE_POS_X] / s_calib_count[FACE_POS_X];  /* Should be -g */
                                x_offset = (x_pos_mean + x_neg_mean) / 2.0f;
                                x_scale = g / fabsf((x_pos_mean - x_neg_mean) / 2.0f);
                            }

                            /* Y axis: +Y face (ay ≈ -g), -Y face (ay ≈ +g) */
                            float y_offset = 0, y_scale = 1.0f;
                            if (s_calib_count[FACE_POS_Y] > 0 && s_calib_count[FACE_NEG_Y] > 0) {
                                float y_pos_mean = s_calib_sum_y[FACE_NEG_Y] / s_calib_count[FACE_NEG_Y];
                                float y_neg_mean = s_calib_sum_y[FACE_POS_Y] / s_calib_count[FACE_POS_Y];
                                y_offset = (y_pos_mean + y_neg_mean) / 2.0f;
                                y_scale = g / fabsf((y_pos_mean - y_neg_mean) / 2.0f);
                            }

                            /* Z axis: +Z face (az ≈ -g), -Z face (az ≈ +g) */
                            float z_offset = 0, z_scale = 1.0f;
                            if (s_calib_count[FACE_POS_Z] > 0 && s_calib_count[FACE_NEG_Z] > 0) {
                                float z_pos_mean = s_calib_sum_z[FACE_NEG_Z] / s_calib_count[FACE_NEG_Z];
                                float z_neg_mean = s_calib_sum_z[FACE_POS_Z] / s_calib_count[FACE_POS_Z];
                                z_offset = (z_pos_mean + z_neg_mean) / 2.0f;
                                z_scale = g / fabsf((z_pos_mean - z_neg_mean) / 2.0f);
                            }

                            /* Write calibration.csv in m/s² (matches protocol doc format) */
                            if (s_calibration_file) {
                                fprintf(s_calibration_file, "x,%.3f,%.3f,m/s²\n", x_offset, x_scale);
                                fprintf(s_calibration_file, "y,%.3f,%.3f,m/s²\n", y_offset, y_scale);
                                fprintf(s_calibration_file, "z,%.3f,%.3f,m/s²\n", z_offset, z_scale);
                                fflush(s_calibration_file);
                                
                                ESP_LOGI(TAG, "Calibration parameters written to calibration.csv");
                                ESP_LOGI(TAG, "X: offset=%.3f, scale=%.3f", x_offset, x_scale);
                                ESP_LOGI(TAG, "Y: offset=%.3f, scale=%.3f", y_offset, y_scale);
                                ESP_LOGI(TAG, "Z: offset=%.3f, scale=%.3f", z_offset, z_scale);
                            }

                            complete_calibration_locked();
                        } else {
                            /* Log transition */
                            ESP_LOGI(TAG, "Face %s complete (%lu samples). Moving to %s",
                                     get_face_name(prev_face), (unsigned long)s_face_sample_count,
                                     get_face_name(s_current_face));
                            snprintf(s_face_name, sizeof(s_face_name), "Face: %s (0s)",
                                     get_face_name(s_current_face));
                        }
                    } else {
                        /* Update face timer display */
                        uint32_t remaining_sec = (FACE_DURATION_MS - elapsed_ms) / 1000;
                        snprintf(s_face_name, sizeof(s_face_name), "Face: %s (%lu s)",
                                 get_face_name(s_current_face), (unsigned long)remaining_sec);
                    }
                }

                /* 6. Frequency measurement */
                if (s_freq_measuring) {
                    s_freq_sample_count++;
                    
                    if (s_freq_sample_count >= FREQ_MEASURE_SAMPLES) {
                        int64_t elapsed_us = esp_timer_get_time() - s_freq_start_time_us;
                        if (elapsed_us > 0) {
                            s_measured_freq_hz = (float)s_freq_sample_count * 1000000.0f / (float)elapsed_us;
                            s_freq_valid = true;
                            
                            int32_t freq_d2 = (int32_t)(s_measured_freq_hz * 100);  /* Hz * 100 for integer log (0.01 Hz precision) */
                            int32_t elapsed_s_d2 = (int32_t)(elapsed_us / 10000);  /* Time in 0.01s units */
                            ESP_LOGI(TAG, "Frequency measured: %" PRId32 " Hz (Target: %d Hz, Samples: %" PRIu32 ", Time: %" PRId32 " s)",
                                     freq_d2, TARGET_SAMPLE_FREQ_HZ, 
                                     s_freq_sample_count, elapsed_s_d2);
                            
                            int32_t freq_error_d2 = abs((int32_t)((s_measured_freq_hz - TARGET_SAMPLE_FREQ_HZ) * 100));
                            if (freq_error_d2 < 500) {  /* 5.00 Hz * 100 */
                                ESP_LOGI(TAG, "✓ Sampling frequency OK (within ±5 Hz of 100 Hz target)");
                            } else {
                                ESP_LOGW(TAG, "✗ Sampling frequency OUT OF RANGE (±5 Hz of 100 Hz target)");
                            }
                        }
                        s_freq_measuring = false;
                    }
                }

                /* 7. Stand protocol state machine */
                if (s_stand_protocol_active) {
                    int64_t now_ms = esp_timer_get_time() / 1000;
                    int64_t elapsed_ms = now_ms - s_stand_phase_start_ms;
                    int group_num = s_stand_group + 1;  /* 1-based for display */

                    switch (s_stand_state) {
                    case STAND_PROTOCOL_PREP:
                        /* 3s prep phase: hold stand pose, label = "stand" */
                        snprintf(s_stand_display, sizeof(s_stand_display),
                                 "Stand: Prep G%d (%ds)", group_num,
                                 (int)((STAND_PREP_DURATION_SEC * 1000 - elapsed_ms) / 1000 + 1));
                        if (elapsed_ms >= STAND_PREP_DURATION_SEC * 1000) {
                            /* Transition to execute phase */
                            s_stand_state = STAND_PROTOCOL_EXECUTE;
                            s_stand_phase_start_ms = now_ms;
                            snprintf(s_stand_label, sizeof(s_stand_label), "stand");
                            ESP_LOGI(TAG, "[Stand] Group %d: PREP done → EXECUTE (%ds)", group_num, s_stand_duration_sec);
                        }
                        break;

    case STAND_PROTOCOL_EXECUTE:
        /* 20-30s execute phase: standing still, label = "stand" */
        snprintf(s_stand_display, sizeof(s_stand_display),
                 "Stand: Exec G%d (%ds)", group_num,
                 (int)((s_stand_duration_sec * 1000 - elapsed_ms) / 1000 + 1));
        if (elapsed_ms >= s_stand_duration_sec * 1000) {
            /* Transition to interval phase */
            s_stand_state = STAND_PROTOCOL_INTERVAL;
            s_stand_phase_start_ms = now_ms;
            snprintf(s_stand_label, sizeof(s_stand_label), "idle");
            ESP_LOGI(TAG, "[Stand] Group %d: EXECUTE done → INTERVAL", group_num);
        }
        break;

                    case STAND_PROTOCOL_INTERVAL: {
                        /* 5s interval: relax, label = "idle" */
                        snprintf(s_stand_display, sizeof(s_stand_display),
                                 "Stand: Rest G%d→%d (%ds)",
                                 group_num, group_num + 1,
                                 (int)((s_stand_interval_sec * 1000 - elapsed_ms) / 1000 + 1));
                        if (elapsed_ms >= s_stand_interval_sec * 1000) {
                            s_stand_group++;
                            if (s_stand_group >= STAND_TOTAL_GROUPS) {
                                /* All groups complete */
                                ESP_LOGI(TAG, "[Stand] All %d groups complete!", STAND_TOTAL_GROUPS);
                                complete_stand_protocol_locked();
                            } else {
                                /* Close previous file and create new file for next group */
                                esp_err_t ret = create_stand_file_for_current_group();
                                if (ret != ESP_OK) {
                                    ESP_LOGE(TAG, "[Stand] Failed to create file for group %d", s_stand_group + 1);
                                    complete_stand_protocol_locked();
                                    break;
                                }
                                
                                /* Start next group's prep phase */
                                s_stand_state = STAND_PROTOCOL_PREP;
                                s_stand_phase_start_ms = now_ms;
                                s_stand_duration_sec = STAND_EXECUTE_DURATION_MIN_SEC + 
                                    (esp_random() % (STAND_EXECUTE_DURATION_MAX_SEC - STAND_EXECUTE_DURATION_MIN_SEC + 1));
                                snprintf(s_stand_label, sizeof(s_stand_label), "stand");
                                ESP_LOGI(TAG, "[Stand] Group %d: REST done → PREP G%d (3s)", 
                                         group_num, s_stand_group + 1);
                            }
                        }
                        break;
                    }

                    default:
                        break;
                    }
                }

                /* Stairs protocol state machine */
                if (s_stairs_protocol_active) {
                    int64_t now_ms = esp_timer_get_time() / 1000;
                    int64_t elapsed_ms = now_ms - s_stairs_phase_start_ms;
                    int group_num = s_stairs_group + 1;  /* 1-based for display */

                    switch (s_stairs_state) {
                    case STAIRS_PROTOCOL_PREP:
                        /* 3s prep phase: hold stairs pose, label = "stairs" */
                        snprintf(s_stairs_display, sizeof(s_stairs_display),
                                 "Stairs: Prep G%d (%ds)", group_num,
                                 (int)((STAIRS_PREP_DURATION_SEC * 1000 - elapsed_ms) / 1000 + 1));
                        if (elapsed_ms >= STAIRS_PREP_DURATION_SEC * 1000) {
                            /* Transition to execute phase */
                            s_stairs_state = STAIRS_PROTOCOL_EXECUTE;
                            s_stairs_phase_start_ms = now_ms;
                            snprintf(s_stairs_label, sizeof(s_stairs_label), "stairs");
                            ESP_LOGI(TAG, "[Stairs] Group %d: PREP done → EXECUTE (25s)", group_num);
                        }
                        break;

                    case STAIRS_PROTOCOL_EXECUTE:
                        /* 20-30s stairs execution: label = "stairs" */
                        snprintf(s_stairs_display, sizeof(s_stairs_display),
                                 "Stairs: Group %d (%ds)", group_num,
                                 (int)((s_stairs_duration_sec * 1000 - elapsed_ms) / 1000 + 1));
                        if (elapsed_ms >= s_stairs_duration_sec * 1000) {
                            /* Transition to interval phase */
                            s_stairs_state = STAIRS_PROTOCOL_INTERVAL;
                            s_stairs_phase_start_ms = now_ms;
                            snprintf(s_stairs_label, sizeof(s_stairs_label), "idle");
                            ESP_LOGI(TAG, "[Stairs] Group %d: DONE", group_num);
                        }
                        break;

                    case STAIRS_PROTOCOL_INTERVAL: {
                        /* 60s rest interval: label = "idle" */
                        int next_group = (group_num < STAIRS_TOTAL_GROUPS) ? group_num + 1 : 0;
                        snprintf(s_stairs_display, sizeof(s_stairs_display),
                                 "Stairs: Rest G%d→%d (%ds)",
                                 group_num, next_group,
                                 (int)((s_stairs_interval_sec * 1000 - elapsed_ms) / 1000 + 1));
                        if (elapsed_ms >= s_stairs_interval_sec * 1000) {
                            s_stairs_group++;
                            if (s_stairs_group >= STAIRS_TOTAL_GROUPS) {
                                /* All groups complete */
                                ESP_LOGI(TAG, "[Stairs] All %d groups complete!", STAIRS_TOTAL_GROUPS);
                                complete_stairs_protocol_locked();
                            } else {
                                /* Create new file for next group */
                                esp_err_t ret = create_stairs_file_for_current_group();
                                if (ret != ESP_OK) {
                                    ESP_LOGE(TAG, "[Stairs] Failed to create file for group %d", s_stairs_group + 1);
                                    complete_stairs_protocol_locked();
                                    break;
                                }
                                /* Start next group's PREP phase */
                                s_stairs_state = STAIRS_PROTOCOL_PREP;
                                s_stairs_phase_start_ms = now_ms;
                                s_stairs_duration_sec = STAIRS_EXECUTE_DURATION_MIN_SEC + 
                                    (esp_random() % (STAIRS_EXECUTE_DURATION_MAX_SEC - STAIRS_EXECUTE_DURATION_MIN_SEC + 1));
                                snprintf(s_stairs_label, sizeof(s_stairs_label), "stairs");
                                ESP_LOGI(TAG, "[Stairs] Group %d: REST done → PREP G%d (3s)", 
                                         group_num, s_stairs_group + 1);
                            }
                        }
                        break;
                    }

                    default:
                        break;
                    }
                }

                /* Bend protocol state machine */
                if (s_bend_protocol_active) {
                    int64_t now_ms = esp_timer_get_time() / 1000;
                    int64_t elapsed_ms = now_ms - s_bend_phase_start_ms;
                    int group_num = s_bend_group + 1;  /* 1-based for display */

                    switch (s_bend_state) {
                    case BEND_PROTOCOL_PREP:
                        /* 3s prep phase: hold bend pose, label = "bend" */
                        snprintf(s_bend_display, sizeof(s_bend_display),
                                 "Bend: Prep G%d (%ds)", group_num,
                                 (int)((BEND_PREP_DURATION_SEC * 1000 - elapsed_ms) / 1000 + 1));
                        if (elapsed_ms >= BEND_PREP_DURATION_SEC * 1000) {
                            /* Transition to execute phase */
                            s_bend_state = BEND_PROTOCOL_EXECUTE;
                            s_bend_phase_start_ms = now_ms;
                            snprintf(s_bend_label, sizeof(s_bend_label), "bend");
                            /* Randomize duration for first group */
                            s_bend_duration_sec = BEND_DURATION_MIN_SEC + 
                                (esp_random() % (BEND_DURATION_MAX_SEC - BEND_DURATION_MIN_SEC + 1));
                            ESP_LOGI(TAG, "[Bend] Group %d: PREP done → EXECUTE (%ds)", group_num, s_bend_duration_sec);
                        }
                        break;

                    case BEND_PROTOCOL_EXECUTE:
                        /* 20-30s bend execution: label = "bend" */
                        snprintf(s_bend_display, sizeof(s_bend_display),
                                 "Bend: Group %d (%ds)", group_num,
                                 (int)((s_bend_duration_sec * 1000 - elapsed_ms) / 1000 + 1));
                        if (elapsed_ms >= s_bend_duration_sec * 1000) {
                            /* Transition to interval phase */
                            s_bend_state = BEND_PROTOCOL_INTERVAL;
                            s_bend_phase_start_ms = now_ms;
                            snprintf(s_bend_label, sizeof(s_bend_label), "idle");
                            /* Randomize duration and interval */
                            s_bend_duration_sec = BEND_DURATION_MIN_SEC + 
                                (esp_random() % (BEND_DURATION_MAX_SEC - BEND_DURATION_MIN_SEC + 1));
                            s_bend_interval_duration_sec = BEND_INTERVAL_MIN_SEC + 
                                (esp_random() % (BEND_INTERVAL_MAX_SEC - BEND_INTERVAL_MIN_SEC + 1));
                            ESP_LOGI(TAG, "[Bend] Group %d: DONE → REST (bend=%ds, rest=%ds)", 
                                     group_num, s_bend_duration_sec, s_bend_interval_duration_sec);
                        }
                        break;

                    case BEND_PROTOCOL_INTERVAL: {
                        /* 60-90s interval phase: rest, label = "idle" */
                        int next_group = (group_num < BEND_TOTAL_GROUPS) ? group_num + 1 : 0;
                        snprintf(s_bend_display, sizeof(s_bend_display),
                                 "Bend: Rest G%d→%d (%ds)",
                                 group_num, next_group,
                                 (int)((s_bend_interval_duration_sec * 1000 - elapsed_ms) / 1000 + 1));
                        if (elapsed_ms >= s_bend_interval_duration_sec * 1000) {
                            s_bend_group++;
                            if (s_bend_group >= BEND_TOTAL_GROUPS) {
                                /* All groups complete */
                                ESP_LOGI(TAG, "[Bend] All %d groups complete!", BEND_TOTAL_GROUPS);
                                complete_bend_protocol_locked();
                            } else {
                                /* Create new file for next group */
                                esp_err_t ret = create_bend_file_for_current_group();
                                if (ret != ESP_OK) {
                                    ESP_LOGE(TAG, "[Bend] Failed to create file for group %d", s_bend_group + 1);
                                    complete_bend_protocol_locked();
                                    break;
                                }
                                /* Start next group's PREP phase */
                                s_bend_state = BEND_PROTOCOL_PREP;
                                s_bend_phase_start_ms = now_ms;
                                snprintf(s_bend_label, sizeof(s_bend_label), "bend");
                                ESP_LOGI(TAG, "[Bend] Group %d: REST done → PREP G%d (3s)", 
                                         group_num, s_bend_group + 1);
                            }
                        }
                        break;
                    }

                    default:
                        break;
                    }
                }

                /* Jump protocol state machine */
                if (s_jump_protocol_active) {
                    int64_t now_ms = esp_timer_get_time() / 1000;
                    int64_t elapsed_ms = now_ms - s_jump_phase_start_ms;
                    int group_num = s_jump_group + 1;  /* 1-based for display */

                    switch (s_jump_state) {
                    case JUMP_PROTOCOL_PREP:
                        /* 3s prep phase: hold jump pose, label = "jump" */
                        snprintf(s_jump_display, sizeof(s_jump_display),
                                 "Jump: Prep G%d (%ds)", group_num,
                                 (int)((JUMP_PREP_DURATION_SEC * 1000 - elapsed_ms) / 1000 + 1));
                        if (elapsed_ms >= JUMP_PREP_DURATION_SEC * 1000) {
                            /* Transition to execute phase */
                            s_jump_state = JUMP_PROTOCOL_EXECUTE;
                            s_jump_phase_start_ms = now_ms;
                            snprintf(s_jump_label, sizeof(s_jump_label), "jump");
                            ESP_LOGI(TAG, "[Jump] Group %d: PREP done → EXECUTE (10s)", group_num);
                        }
                        break;

                    case JUMP_PROTOCOL_EXECUTE:
                        /* 5-10s jump execution: label = "jump" */
                        snprintf(s_jump_display, sizeof(s_jump_display),
                                 "Jump: Group %d (%ds)", group_num,
                                 (int)((s_jump_duration_sec * 1000 - elapsed_ms) / 1000 + 1));
                        if (elapsed_ms >= s_jump_duration_sec * 1000) {
                            /* Transition to interval phase */
                            s_jump_state = JUMP_PROTOCOL_INTERVAL;
                            s_jump_phase_start_ms = now_ms;
                            snprintf(s_jump_label, sizeof(s_jump_label), "idle");
                            /* Randomize interval between 5-10s */
                            s_jump_interval_duration_sec = JUMP_INTERVAL_MIN_SEC + 
                                (esp_random() % (JUMP_INTERVAL_MAX_SEC - JUMP_INTERVAL_MIN_SEC + 1));
                            ESP_LOGI(TAG, "[Jump] Group %d: DONE → REST (%ds idle)", 
                                     group_num, s_jump_interval_duration_sec);
                        }
                        break;

                    case JUMP_PROTOCOL_INTERVAL: {
                        /* 5-10s interval phase: rest, label = "idle" */
                        int next_group = (group_num < JUMP_TOTAL_GROUPS) ? group_num + 1 : 0;
                        snprintf(s_jump_display, sizeof(s_jump_display),
                                 "Jump: Rest G%d→%d (%ds)",
                                 group_num, next_group,
                                 (int)((s_jump_interval_duration_sec * 1000 - elapsed_ms) / 1000 + 1));
                        if (elapsed_ms >= s_jump_interval_duration_sec * 1000) {
                            s_jump_group++;
                            if (s_jump_group >= JUMP_TOTAL_GROUPS) {
                                /* All groups complete */
                                ESP_LOGI(TAG, "[Jump] All %d groups complete!", JUMP_TOTAL_GROUPS);
                                complete_jump_protocol_locked();
                            } else {
                                /* Close current group file and open next group file */
                                if (s_jump_data_file) {
                                    fclose(s_jump_data_file);
                                    s_jump_data_file = NULL;
                                }
                                
                                snprintf(s_jump_file_path, sizeof(s_jump_file_path),
                                         BSP_SD_MOUNT_POINT "/jump_%d.csv",
                                         s_jump_group + 1);
                                
                                s_jump_data_file = fopen(s_jump_file_path, "w");
                                if (!s_jump_data_file) {
                                    ESP_LOGE(TAG, "[Jump] Failed to open group %d file", s_jump_group + 1);
                                    complete_jump_protocol_locked();
                                    break;
                                }
                                
                                fprintf(s_jump_data_file, "label,timestamp_ms,accel_x,accel_y,accel_z\n");
                                fflush(s_jump_data_file);
                                
                                /* Sync data_file to jump_data_file so sampler_task can write */
                                s_data_file = s_jump_data_file;
                                
                                /* Start next group's PREP phase */
                                s_jump_state = JUMP_PROTOCOL_PREP;
                                s_jump_phase_start_ms = now_ms;
                                snprintf(s_jump_label, sizeof(s_jump_label), "jump");
                                ESP_LOGI(TAG, "[Jump] Group %d: REST done → PREP G%d (3s)", 
                                         group_num + 1, s_jump_group + 1);
                            }
                        }
                        break;
                    }

                    default:
                        break;
                    }
                }

                /* Fall protocol state machine */
                if (s_fall_protocol_active) {
                    int64_t now_ms = esp_timer_get_time() / 1000;
                    int64_t elapsed_ms = now_ms - s_fall_phase_start_ms;
                    int group_num = s_fall_group + 1;  /* 1-based for display */

                    switch (s_fall_state) {
                    case FALL_PROTOCOL_PREP:
                        /* 3s prep phase: hold fall pose, label = "fall" */
                        snprintf(s_fall_display, sizeof(s_fall_display),
                                 "Fall: Prep G%d (%ds)", group_num,
                                 (int)((FALL_PREP_DURATION_SEC * 1000 - elapsed_ms) / 1000 + 1));
                        if (elapsed_ms >= FALL_PREP_DURATION_SEC * 1000) {
                            /* Transition to execute phase */
                            s_fall_state = FALL_PROTOCOL_EXECUTE;
                            s_fall_phase_start_ms = now_ms;
                            snprintf(s_fall_label, sizeof(s_fall_label), "fall");
                            /* Randomize duration for first group */
                            s_fall_duration_sec = FALL_EXECUTE_DURATION_MIN_SEC + 
                                (esp_random() % (FALL_EXECUTE_DURATION_MAX_SEC - FALL_EXECUTE_DURATION_MIN_SEC + 1));
                            ESP_LOGI(TAG, "[Fall] Group %d: PREP done → EXECUTE (%ds)", group_num, s_fall_duration_sec);
                        }
                        break;

                    case FALL_PROTOCOL_EXECUTE:
                        /* 5-10s fall execution: label = "fall" */
                        snprintf(s_fall_display, sizeof(s_fall_display),
                                 "Fall: Group %d (%ds)", group_num,
                                 (int)((s_fall_duration_sec * 1000 - elapsed_ms) / 1000 + 1));
                        if (elapsed_ms >= s_fall_duration_sec * 1000) {
                            /* Transition to interval phase */
                            s_fall_state = FALL_PROTOCOL_INTERVAL;
                            s_fall_phase_start_ms = now_ms;
                            snprintf(s_fall_label, sizeof(s_fall_label), "idle");
                            /* Randomize duration and interval */
                            s_fall_duration_sec = FALL_EXECUTE_DURATION_MIN_SEC + 
                                (esp_random() % (FALL_EXECUTE_DURATION_MAX_SEC - FALL_EXECUTE_DURATION_MIN_SEC + 1));
                            s_fall_interval_duration_sec = FALL_INTERVAL_MIN_SEC + 
                                (esp_random() % (FALL_INTERVAL_MAX_SEC - FALL_INTERVAL_MIN_SEC + 1));
                            ESP_LOGI(TAG, "[Fall] Group %d: DONE → REST (fall=%ds, rest=%ds)", 
                                     group_num, s_fall_duration_sec, s_fall_interval_duration_sec);
                        }
                        break;

                    case FALL_PROTOCOL_INTERVAL: {
                        /* 15-30s interval phase: rest, label = "idle" */
                        int next_group = (group_num < FALL_TOTAL_GROUPS) ? group_num + 1 : 0;
                        snprintf(s_fall_display, sizeof(s_fall_display),
                                 "Fall: Rest G%d→%d (%ds)",
                                 group_num, next_group,
                                 (int)((s_fall_interval_duration_sec * 1000 - elapsed_ms) / 1000 + 1));
                        if (elapsed_ms >= s_fall_interval_duration_sec * 1000) {
                            s_fall_group++;
                            if (s_fall_group >= FALL_TOTAL_GROUPS) {
                                /* All groups complete */
                                ESP_LOGI(TAG, "[Fall] All %d groups complete!", FALL_TOTAL_GROUPS);
                                complete_fall_protocol_locked();
                            } else {
                                /* Create new file for next group */
                                snprintf(s_file_path, sizeof(s_file_path),
                                         BSP_SD_MOUNT_POINT "/fall_%d.csv",
                                         s_fall_group + 1);
                                s_data_file = fopen(s_file_path, "w");
                                if (!s_data_file) {
                                    ESP_LOGE(TAG, "[Fall] Failed to open group %d file", s_fall_group + 1);
                                    complete_fall_protocol_locked();
                                    break;
                                }
                                fprintf(s_data_file, "label,timestamp_ms,accel_x,accel_y,accel_z\n");
                                fflush(s_data_file);
                                /* Start next group's PREP phase */
                                s_fall_state = FALL_PROTOCOL_PREP;
                                s_fall_phase_start_ms = now_ms;
                                snprintf(s_fall_label, sizeof(s_fall_label), "fall");
                                ESP_LOGI(TAG, "[Fall] Group %d: REST done → PREP G%d (3s)", 
                                         group_num + 1, s_fall_group + 1);
                            }
                        }
                        break;
                    }

                    default:
                        break;
                    }
                }
            }
            xSemaphoreGive(s_state_mutex);
        }

        /* Drift-free timing: wait until absolute target time using esp_timer delay */
        int64_t now_us = esp_timer_get_time();
        int64_t delay_us = next_wake_us - now_us;
        if (delay_us > 0) {
            /* Use precise us delay when < 2ms, otherwise use vTaskDelay */
            if (delay_us < 2000) {
                esp_rom_delay_us((uint32_t)delay_us);
            } else {
                vTaskDelay(pdMS_TO_TICKS((delay_us + 500) / 1000));  /* rounded */
            }
        }
        /* Advance to next period (phase accumulator — prevents drift) */
        next_wake_us += (SAMPLE_PERIOD_MS * 1000);

        /* Periodically refresh UI (every 10 samples = ~100ms) */
        if (s_sample_count % 10 == 0) {
            refresh_ui();
        }
    }
}

/* ================================================================
 *  Main — Boot Sequence
 * ================================================================ */
void app_main(void)
{
    /* 0. Mutex */
    s_state_mutex = xSemaphoreCreateMutex();
    assert(s_state_mutex != NULL);

    if (xSemaphoreTake(s_state_mutex, pdMS_TO_TICKS(50)) == pdTRUE) {
        set_status_locked("Booting");
        s_file_path[0] = '\0';
        xSemaphoreGive(s_state_mutex);
    }

    /* 1. NVS (required by WiFi) */
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    /* 2. LVGL UI */
    create_ui();

    /* 3. Wi-Fi + SNTP (blocking — waits for connection + time) */
    wifi_init_sta();         // blocks until connected or failed
    initialize_sntp();       // blocks until time synced or timeout

    /* 4. SD card */
    init_sdcard();

    /* 5. Accelerometer */
    if (app_accel_init() == ESP_OK) {
        ESP_LOGI(TAG, "Accelerometer initialized");
        if (xSemaphoreTake(s_state_mutex, pdMS_TO_TICKS(200)) == pdTRUE) {
            set_status_locked("IMU ready");
            xSemaphoreGive(s_state_mutex);
        }
    } else {
        ESP_LOGW(TAG, "Accelerometer not found, continuing without IMU");
        if (xSemaphoreTake(s_state_mutex, pdMS_TO_TICKS(200)) == pdTRUE) {
            set_status_locked("No IMU found");
            xSemaphoreGive(s_state_mutex);
        }
    }

    /* 6. Buttons */
    init_buttons();

    /* 7. Refresh UI once */
    refresh_ui();

    /* 8. Start sampler task */
    xTaskCreate(sampler_task, "sampler_task", 6144, NULL, 5, NULL);

    ESP_LOGI(TAG, "System ready. Button A: start/stop normal IMU logging");
    ESP_LOGI(TAG, "Protocol toggle: Long-press Button B (2s) cycles through 5 protocols");
    ESP_LOGI(TAG, "  → Stand: 3x(prep 3s + stand 20-30s + idle 5s), CSV: stand_1/2/3.csv");
    ESP_LOGI(TAG, "  → Stairs: 3x(prep 3s + stairs 20-30s + idle 60s), CSV: stairs_1/2/3.csv");
    ESP_LOGI(TAG, "  → Bend: 3x(prep 3s + bend 20-30s + idle 60-90s), CSV: bend_1/2/3.csv");
    ESP_LOGI(TAG, "  → Jump: 3x(prep 3s + jump 5-10s + idle 5-10s), CSV: jump_1/2/3.csv");
    ESP_LOGI(TAG, "  → Fall: 3x(prep 3s + fall 5-10s + idle 15-30s), CSV: fall_1/2/3.csv");
    ESP_LOGI(TAG, "Data format: CSV (UTF-8), Fields: label, timestamp(ms), x(m/s²), y(m/s²), z(m/s²)");
    ESP_LOGI(TAG, "Target sampling frequency: %d Hz", TARGET_SAMPLE_FREQ_HZ);
}
