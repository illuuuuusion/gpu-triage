#ifndef GPU_TRIAGE_ANALYSIS_H
#define GPU_TRIAGE_ANALYSIS_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define GT_PATTERN_VERSION 1u
#define GT_PATTERN_COUNT 9u
#define GT_MAX_TRACKED_ERRORS 4096u

typedef struct {
    uint64_t total;
    uint64_t recorded;
    uint64_t first_offset;
    uint64_t last_offset;
    uint64_t bit_0_to_1[32];
    uint64_t bit_1_to_0[32];
    uint64_t xor_bits[32];
    uint64_t offsets[GT_MAX_TRACKED_ERRORS];
    uint32_t offset_hits[GT_MAX_TRACKED_ERRORS];
    uint8_t allocation_mask[GT_MAX_TRACKED_ERRORS];
    uint8_t pass_mask[GT_MAX_TRACKED_ERRORS];
    uint8_t reread_mask[GT_MAX_TRACKED_ERRORS];
    size_t tracked;
} gt_error_summary;

const char *gt_pattern_name(uint32_t pattern);
uint32_t gt_pattern_value(uint32_t pattern, uint32_t index, uint32_t seed);
void gt_fill_pattern(uint32_t *dst, size_t words, uint32_t pattern,
                     uint32_t seed, uint32_t stride);
void gt_summary_init(gt_error_summary *summary);
void gt_summary_add(gt_error_summary *summary, uint64_t offset,
                    uint32_t expected, uint32_t actual,
                    uint32_t allocation, uint32_t pass, uint32_t reread);
uint64_t gt_stride_candidate(const gt_error_summary *summary);
uint64_t gt_cluster_count(const gt_error_summary *summary, uint64_t gap);
uint64_t gt_reproducible_reread(const gt_error_summary *summary);
uint64_t gt_reproducible_pass(const gt_error_summary *summary);
uint64_t gt_reproducible_allocation(const gt_error_summary *summary);

#endif
