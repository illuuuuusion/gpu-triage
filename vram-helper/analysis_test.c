#include "analysis.h"

#include <assert.h>
#include <stdint.h>
#include <string.h>

int main(void) {
    uint32_t a[128], b[128];
    for (uint32_t pattern = 0; pattern < GT_PATTERN_COUNT; ++pattern) {
        gt_fill_pattern(a, 128, pattern, 1234 + pattern, 17);
        memset(b, 0, sizeof(b));
        for (uint32_t logical = 0; logical < 128; ++logical)
            b[(logical * 17u) % 128u] = gt_pattern_value(pattern, logical, 1234 + pattern);
        assert(!memcmp(a, b, sizeof(a)));
    }
    gt_error_summary s; gt_summary_init(&s);
    gt_summary_add(&s, 64, 0xaaaaaaaa, 0xaaa8aaaa, 0, 0, 0);
    gt_summary_add(&s, 64, 0xaaaaaaaa, 0xaaa8aaaa, 0, 1, 1);
    gt_summary_add(&s, 64, 0xaaaaaaaa, 0xaaa8aaaa, 1, 2, 0);
    gt_summary_add(&s, 128, 0, 1, 0, 0, 0);
    assert(s.total == 4 && s.xor_bits[17] == 3 && s.bit_1_to_0[17] == 3);
    assert(s.bit_0_to_1[0] == 1 && gt_cluster_count(&s, 64) == 1);
    assert(gt_stride_candidate(&s) == 64);
    assert(gt_reproducible_reread(&s) == 1);
    assert(gt_reproducible_pass(&s) == 1);
    assert(gt_reproducible_allocation(&s) == 1);
    return 0;
}
