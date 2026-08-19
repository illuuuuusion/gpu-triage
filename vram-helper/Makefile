CC ?= cc
GLSLANG ?= glslangValidator
OUT_DIR ?= build
CFLAGS ?= -O2 -g0
CFLAGS += -std=c17 -Wall -Wextra -Wpedantic -Werror
LDLIBS += -lvulkan

BIN := $(OUT_DIR)/gpu-triage-vram-helper
SPV := $(OUT_DIR)/shaders/pattern.spv

.PHONY: all clean check
all: $(BIN) $(SPV)

$(OUT_DIR):
	mkdir -p $@

$(OUT_DIR)/shaders:
	mkdir -p $@

$(SPV): shaders/pattern.comp | $(OUT_DIR)/shaders
	$(GLSLANG) -V --target-env vulkan1.1 -o $@ $<

$(BIN): gpu_vram_helper.c analysis.c analysis.h | $(OUT_DIR)
	$(CC) $(CFLAGS) -I. gpu_vram_helper.c analysis.c -o $@ $(LDLIBS)

check: all
	$(BIN) --self-test >/dev/null

clean:
	rm -rf -- $(OUT_DIR)
