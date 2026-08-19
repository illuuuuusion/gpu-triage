#include "analysis.h"

#include <limits.h>
#include <string.h>

static uint32_t hash32(uint32_t value) {
    value ^= value >> 16;
    value *= 0x7feb352du;
    value ^= value >> 15;
    value *= 0x846ca68bu;
    return value ^ (value >> 16);
}

const char *gt_pattern_name(uint32_t pattern) {
    static const char *names[GT_PATTERN_COUNT] = {
        "all_zero", "all_one", "alternating_aa", "alternating_55",
        "walking_one", "walking_zero", "allocation_offset",
        "allocation_offset_inverse", "deterministic_prng"
    };
    return pattern < GT_PATTERN_COUNT ? names[pattern] : "compute_kat";
}

uint32_t gt_pattern_value(uint32_t pattern, uint32_t index, uint32_t seed) {
    switch (pattern) {
    case 0: return 0u;
    case 1: return UINT32_MAX;
    case 2: return 0xaaaaaaaau;
    case 3: return 0x55555555u;
    case 4: return 1u << (seed & 31u);
    case 5: return ~(1u << (seed & 31u));
    case 6: return (index * 4u) ^ seed;
    case 7: return ~((index * 4u) ^ seed);
    case 8: return hash32(index ^ seed);
    default: return (index * 1664525u + 1013904223u) ^ seed;
    }
}

void gt_fill_pattern(uint32_t *dst, size_t words, uint32_t pattern,
                     uint32_t seed, uint32_t stride) {
    size_t logical;
    if (stride == 0) stride = 1;
    for (logical = 0; logical < words; ++logical) {
        size_t physical = (logical * (size_t)stride) % words;
        dst[physical] = gt_pattern_value(pattern, (uint32_t)logical, seed);
    }
}

void gt_summary_init(gt_error_summary *summary) {
    memset(summary, 0, sizeof(*summary));
    summary->first_offset = UINT64_MAX;
}

void gt_summary_add(gt_error_summary *summary, uint64_t offset,
                    uint32_t expected, uint32_t actual,
                    uint32_t allocation, uint32_t pass, uint32_t reread) {
    uint32_t xor_value = expected ^ actual;
    size_t index;
    summary->total++;
    if (offset < summary->first_offset) summary->first_offset = offset;
    if (offset > summary->last_offset) summary->last_offset = offset;
    for (uint32_t bit = 0; bit < 32; ++bit) {
        uint32_t mask = 1u << bit;
        if (xor_value & mask) summary->xor_bits[bit]++;
        if (!(expected & mask) && (actual & mask)) summary->bit_0_to_1[bit]++;
        if ((expected & mask) && !(actual & mask)) summary->bit_1_to_0[bit]++;
    }
    for (index = 0; index < summary->tracked; ++index)
        if (summary->offsets[index] == offset) break;
    if (index == summary->tracked && summary->tracked < GT_MAX_TRACKED_ERRORS) {
        summary->offsets[index] = offset;
        summary->tracked++;
    }
    if (index < summary->tracked) {
        summary->offset_hits[index]++;
        if (allocation < 8) summary->allocation_mask[index] |= (uint8_t)(1u << allocation);
        if (pass < 8) summary->pass_mask[index] |= (uint8_t)(1u << pass);
        if (reread < 8) summary->reread_mask[index] |= (uint8_t)(1u << reread);
    }
}

static unsigned popcount8(uint8_t value) {
    unsigned count = 0;
    while (value) { count += value & 1u; value >>= 1; }
    return count;
}

uint64_t gt_stride_candidate(const gt_error_summary *summary) {
    uint64_t best = 0, best_hits = 0;
    for (size_t i = 0; i < summary->tracked; ++i) {
        for (size_t j = i + 1; j < summary->tracked; ++j) {
            uint64_t a = summary->offsets[i], b = summary->offsets[j];
            uint64_t delta = a > b ? a - b : b - a;
            uint64_t hits = 1;
            if (!delta) continue;
            for (size_t k = 0; k < summary->tracked; ++k)
                for (size_t m = k + 1; m < summary->tracked; ++m) {
                    uint64_t c = summary->offsets[k], d = summary->offsets[m];
                    if ((c > d ? c - d : d - c) == delta) hits++;
                }
            if (hits > best_hits || (hits == best_hits && delta < best)) {
                best_hits = hits; best = delta;
            }
        }
    }
    return best;
}

uint64_t gt_cluster_count(const gt_error_summary *summary, uint64_t gap) {
    uint64_t sorted[GT_MAX_TRACKED_ERRORS], clusters = 0;
    if (!summary->tracked) return 0;
    memcpy(sorted, summary->offsets, summary->tracked * sizeof(uint64_t));
    for (size_t i = 1; i < summary->tracked; ++i) {
        uint64_t value = sorted[i]; size_t j = i;
        while (j && sorted[j - 1] > value) { sorted[j] = sorted[j - 1]; --j; }
        sorted[j] = value;
    }
    clusters = 1;
    for (size_t i = 1; i < summary->tracked; ++i)
        if (sorted[i] - sorted[i - 1] > gap) clusters++;
    return clusters;
}

static uint64_t reproducible(const gt_error_summary *summary, int kind) {
    uint64_t count = 0;
    for (size_t i = 0; i < summary->tracked; ++i) {
        uint8_t mask = kind == 0 ? summary->reread_mask[i] :
                       kind == 1 ? summary->pass_mask[i] : summary->allocation_mask[i];
        if (popcount8(mask) > 1) count++;
    }
    return count;
}

uint64_t gt_reproducible_reread(const gt_error_summary *s) { return reproducible(s, 0); }
uint64_t gt_reproducible_pass(const gt_error_summary *s) { return reproducible(s, 1); }
uint64_t gt_reproducible_allocation(const gt_error_summary *s) { return reproducible(s, 2); }
