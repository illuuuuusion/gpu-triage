#define _POSIX_C_SOURCE 200809L

#include "analysis.h"

#include <vulkan/vulkan.h>

#include <dirent.h>
#include <errno.h>
#include <inttypes.h>
#include <limits.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#define HELPER_VERSION "1.0.0"
#define ARRAY_LEN(x) (sizeof(x) / sizeof((x)[0]))

typedef struct {
    char bdf[16];
    uint32_t domain, bus, slot, function, vendor, device;
    uint64_t seconds, max_bytes, max_errors;
    uint32_t max_vram_percent;
    int64_t max_temp_mc;
    const char *shader;
    bool self_test;
} options;

typedef struct {
    VkBuffer buffer;
    VkDeviceMemory memory;
    void *mapped;
} buffer;

typedef struct {
    VkInstance instance;
    VkPhysicalDevice physical;
    VkDevice device;
    VkQueue queue;
    uint32_t queue_family;
    VkCommandPool command_pool;
    VkCommandBuffer command;
    VkDescriptorSetLayout descriptor_layout;
    VkDescriptorPool descriptor_pool;
    VkDescriptorSet descriptor;
    VkPipelineLayout pipeline_layout;
    VkPipeline pipeline;
    VkShaderModule shader_module;
    VkPhysicalDeviceMemoryProperties memory;
    buffer host_in, host_out, local_a, local_b;
    VkDeviceSize bytes;
    bool device_lost;
} vk_state;

typedef struct {
    uint64_t comparisons;
    uint64_t errors;
    const char *status;
} experiment;

typedef struct {
    uint32_t words, pattern, seed, stride;
} push_params;

static uint64_t start_ms;

static uint64_t monotonic_ms(void) {
    struct timespec value;
    clock_gettime(CLOCK_MONOTONIC, &value);
    return (uint64_t)value.tv_sec * 1000u + (uint64_t)value.tv_nsec / 1000000u;
}

static void emit_meta(const options *o) {
    printf("{\"type\":\"meta\",\"schema\":1,\"helper\":\"gpu-triage-vram-helper\","
           "\"version\":\"%s\",\"pattern_version\":%u,\"prng\":\"hash32-v1\","
           "\"requested\":{\"seconds\":%" PRIu64 ",\"bytes\":%" PRIu64
           ",\"max_error_records\":%" PRIu64 ",\"max_vram_percent\":%u}}\n",
           HELPER_VERSION, GT_PATTERN_VERSION, o->seconds, o->max_bytes,
           o->max_errors, o->max_vram_percent);
    fflush(stdout);
}

static void emit_identity(const options *o, bool exact, const char *source,
                          const char *name) {
    printf("{\"type\":\"identity\",\"exact_match\":%s,\"bdf\":\"%s\","
           "\"vendor_id\":%u,\"device_id\":%u,\"mapping_source\":%s,"
           "\"name\":\"%s\"}\n", exact ? "true" : "false", o->bdf,
           o->vendor, o->device, source ? "\"VK_EXT_pci_bus_info\"" : "null",
           name ? name : "unavailable");
    fflush(stdout);
}

static void emit_experiment(const char *name, const experiment *e) {
    printf("{\"type\":\"experiment\",\"name\":\"%s\",\"status\":\"%s\","
           "\"comparisons\":%" PRIu64 ",\"errors\":%" PRIu64 "}\n",
           name, e->status, e->comparisons, e->errors);
    fflush(stdout);
}

static void bit_array(uint32_t value) {
    bool first = true;
    putchar('[');
    for (uint32_t bit = 0; bit < 32; ++bit) if (value & (1u << bit)) {
        printf("%s%u", first ? "" : ",", bit); first = false;
    }
    putchar(']');
}

static void emit_error(gt_error_summary *summary, const options *o,
                       uint32_t allocation, uint64_t offset, uint32_t expected,
                       uint32_t actual, const char *pattern, uint32_t seed,
                       uint32_t pass, uint32_t reread, int64_t temp_mc) {
    uint32_t up = (~expected) & actual, down = expected & (~actual);
    gt_summary_add(summary, offset, expected, actual, allocation, pass, reread);
    if (summary->recorded >= o->max_errors) return;
    summary->recorded++;
    printf("{\"type\":\"error\",\"allocation\":%u,\"offset\":%" PRIu64
           ",\"width_bits\":32,\"expected\":\"0x%08" PRIx32
           "\",\"actual\":\"0x%08" PRIx32 "\",\"xor\":\"0x%08" PRIx32
           "\",\"bits_0_to_1\":", allocation, offset, expected, actual,
           expected ^ actual);
    bit_array(up);
    printf(",\"bits_1_to_0\":");
    bit_array(down);
    printf(",\"pattern\":\"%s\",\"seed\":%u,\"pass\":%u,\"reread\":%u,"
           "\"timestamp_ms\":%" PRIu64 ",\"temp_mC\":",
           pattern, seed, pass, reread, monotonic_ms() - start_ms);
    if (temp_mc >= 0) printf("%" PRId64, temp_mc); else printf("null");
    printf("}\n");
    fflush(stdout);
}

static void print_histogram(const uint64_t values[32]) {
    putchar('{');
    bool first = true;
    for (unsigned i = 0; i < 32; ++i) if (values[i]) {
        printf("%s\"%u\":%" PRIu64, first ? "" : ",", i, values[i]);
        first = false;
    }
    putchar('}');
}

static void emit_summary(const options *o, const experiment e[4],
                         const gt_error_summary *s, uint64_t bytes,
                         const char *temp_status, int64_t max_temp,
                         bool device_lost) {
    const char *overall = device_lost ? "INCONCLUSIVE" : "PASS";
    for (size_t i = 0; i < 4; ++i) {
        if (!strcmp(e[i].status, "FAIL")) overall = "FAIL";
        else if (strcmp(e[i].status, "PASS") && strcmp(overall, "FAIL")) overall = "INCONCLUSIVE";
    }
    printf("{\"type\":\"summary\",\"status\":\"%s\",\"device_lost\":%s,"
           "\"experiments\":{\"host_transfer\":\"%s\",\"gpu_local_copy\":\"%s\","
           "\"compute_kat\":\"%s\",\"vram_pattern\":\"%s\"},"
           "\"limits\":{\"seconds\":%" PRIu64 ",\"bytes\":%" PRIu64
           ",\"max_error_records\":%" PRIu64 ",\"max_vram_percent\":%u},"
           "\"temperature\":{\"status\":\"%s\",\"maximum_mC\":",
           overall, device_lost ? "true" : "false", e[0].status, e[1].status,
           e[2].status, e[3].status, o->seconds, bytes, o->max_errors,
           o->max_vram_percent, temp_status);
    if (max_temp >= 0) printf("%" PRId64, max_temp); else printf("null");
    printf("},\"error_summary\":{\"total\":%" PRIu64 ",\"recorded\":%" PRIu64
           ",\"first_offset\":", s->total, s->recorded);
    if (s->total) printf("%" PRIu64, s->first_offset); else printf("null");
    printf(",\"last_offset\":");
    if (s->total) printf("%" PRIu64, s->last_offset); else printf("null");
    printf(",\"xor_bit_histogram\":"); print_histogram(s->xor_bits);
    printf(",\"bits_0_to_1\":"); print_histogram(s->bit_0_to_1);
    printf(",\"bits_1_to_0\":"); print_histogram(s->bit_1_to_0);
    printf(",\"clusters_64b\":%" PRIu64 ",\"stride_candidate_bytes\":%" PRIu64
           ",\"reproducible\":{\"reread\":%" PRIu64 ",\"pass\":%" PRIu64
           ",\"allocation\":%" PRIu64 "}}}\n",
           gt_cluster_count(s, 64), gt_stride_candidate(s),
           gt_reproducible_reread(s), gt_reproducible_pass(s),
           gt_reproducible_allocation(s));
    fflush(stdout);
}

static bool parse_u64(const char *text, uint64_t *result) {
    char *end = NULL; errno = 0;
    unsigned long long value = strtoull(text, &end, 0);
    if (errno || !end || *end) return false;
    *result = (uint64_t)value; return true;
}

static bool parse_bdf(options *o, const char *text) {
    unsigned domain, bus, slot, function; char tail;
    if (sscanf(text, "%4x:%2x:%2x.%1x%c", &domain, &bus, &slot, &function, &tail) != 4 ||
        domain > 0xffff || bus > 0xff || slot > 0x1f || function > 7) return false;
    snprintf(o->bdf, sizeof(o->bdf), "%04x:%02x:%02x.%x", domain, bus, slot, function);
    o->domain = domain; o->bus = bus; o->slot = slot; o->function = function;
    return !strcmp(o->bdf, text);
}

static bool parse_options(int argc, char **argv, options *o) {
    memset(o, 0, sizeof(*o));
    o->seconds = 60; o->max_bytes = 256u * 1024u * 1024u;
    o->max_errors = 256; o->max_temp_mc = 95000; o->max_vram_percent = 25;
    for (int i = 1; i < argc; ++i) {
        if (!strcmp(argv[i], "--self-test")) { o->self_test = true; continue; }
        if (i + 1 >= argc) return false;
        const char *value = argv[++i]; uint64_t number;
        if (!strcmp(argv[i - 1], "--gpu")) { if (!parse_bdf(o, value)) return false; }
        else if (!strcmp(argv[i - 1], "--vendor")) { if (!parse_u64(value, &number) || number > 0xffff) return false; o->vendor = (uint32_t)number; }
        else if (!strcmp(argv[i - 1], "--device")) { if (!parse_u64(value, &number) || number > 0xffff) return false; o->device = (uint32_t)number; }
        else if (!strcmp(argv[i - 1], "--seconds")) { if (!parse_u64(value, &o->seconds)) return false; }
        else if (!strcmp(argv[i - 1], "--max-bytes")) { if (!parse_u64(value, &o->max_bytes)) return false; }
        else if (!strcmp(argv[i - 1], "--max-errors")) { if (!parse_u64(value, &o->max_errors)) return false; }
        else if (!strcmp(argv[i - 1], "--max-vram-percent")) { if (!parse_u64(value, &number) || number > 50) return false; o->max_vram_percent = (uint32_t)number; }
        else if (!strcmp(argv[i - 1], "--max-temp-mc")) { if (!parse_u64(value, &number) || number > INT64_MAX) return false; o->max_temp_mc = (int64_t)number; }
        else if (!strcmp(argv[i - 1], "--shader")) o->shader = value;
        else return false;
    }
    if (o->self_test) {
        strcpy(o->bdf, "0000:00:00.0"); o->vendor = 0x1002; o->device = 0x73af;
        o->seconds = 1; o->max_bytes = 4096; o->max_errors = 8;
        return true;
    }
    return o->bdf[0] && o->vendor && o->device && o->seconds >= 1 &&
           o->max_bytes >= 4096 && o->max_errors >= 1 && o->max_vram_percent >= 1;
}

static bool has_extension(VkPhysicalDevice physical, const char *wanted) {
    uint32_t count = 0;
    if (vkEnumerateDeviceExtensionProperties(physical, NULL, &count, NULL) != VK_SUCCESS) return false;
    VkExtensionProperties *items = calloc(count, sizeof(*items));
    if (!items) return false;
    bool found = false;
    if (vkEnumerateDeviceExtensionProperties(physical, NULL, &count, items) == VK_SUCCESS)
        for (uint32_t i = 0; i < count; ++i)
            if (!strcmp(items[i].extensionName, wanted)) found = true;
    free(items); return found;
}

static bool drm_matches_bdf(int64_t major, int64_t minor, const char *bdf) {
    char path[PATH_MAX], resolved[PATH_MAX];
    snprintf(path, sizeof(path), "/sys/dev/char/%" PRId64 ":%" PRId64 "/device", major, minor);
    if (!realpath(path, resolved)) return false;
    char needle[32]; snprintf(needle, sizeof(needle), "/%s/", bdf);
    size_t len = strlen(resolved), nlen = strlen(bdf);
    return strstr(resolved, needle) != NULL || (len >= nlen && !strcmp(resolved + len - nlen, bdf));
}

static bool select_physical(vk_state *v, const options *o, char name[VK_MAX_PHYSICAL_DEVICE_NAME_SIZE],
                            const char **mapping_source) {
    uint32_t count = 0, matches = 0;
    if (vkEnumeratePhysicalDevices(v->instance, &count, NULL) != VK_SUCCESS || !count) return false;
    VkPhysicalDevice *devices = calloc(count, sizeof(*devices));
    if (!devices || vkEnumeratePhysicalDevices(v->instance, &count, devices) != VK_SUCCESS) { free(devices); return false; }
    for (uint32_t i = 0; i < count; ++i) {
        bool pci_ext = has_extension(devices[i], VK_EXT_PCI_BUS_INFO_EXTENSION_NAME);
#ifdef VK_EXT_physical_device_drm
        bool drm_ext = has_extension(devices[i], VK_EXT_PHYSICAL_DEVICE_DRM_EXTENSION_NAME);
#else
        bool drm_ext = false;
#endif
        VkPhysicalDevicePCIBusInfoPropertiesEXT pci = {
            .sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PCI_BUS_INFO_PROPERTIES_EXT
        };
#ifdef VK_EXT_physical_device_drm
        VkPhysicalDeviceDrmPropertiesEXT drm = {
            .sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_DRM_PROPERTIES_EXT
        };
        if (drm_ext) drm.pNext = pci_ext ? &pci : NULL;
#endif
        VkPhysicalDeviceProperties2 props = { .sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PROPERTIES_2 };
#ifdef VK_EXT_physical_device_drm
        props.pNext = drm_ext ? (void *)&drm : pci_ext ? (void *)&pci : NULL;
#else
        props.pNext = pci_ext ? (void *)&pci : NULL;
#endif
        vkGetPhysicalDeviceProperties2(devices[i], &props);
        if (props.properties.vendorID != o->vendor || props.properties.deviceID != o->device) continue;
        bool exact_pci = pci_ext && pci.pciDomain == o->domain && pci.pciBus == o->bus &&
                         pci.pciDevice == o->slot && pci.pciFunction == o->function;
        bool exact_drm = false;
#ifdef VK_EXT_physical_device_drm
        if (drm_ext && drm.hasRender)
            exact_drm = drm_matches_bdf(drm.renderMajor, drm.renderMinor, o->bdf);
        if (!exact_drm && drm_ext && drm.hasPrimary)
            exact_drm = drm_matches_bdf(drm.primaryMajor, drm.primaryMinor, o->bdf);
#endif
        if (exact_pci || exact_drm) {
            v->physical = devices[i];
            snprintf(name, VK_MAX_PHYSICAL_DEVICE_NAME_SIZE, "%s", props.properties.deviceName);
            *mapping_source = exact_pci ? "VK_EXT_pci_bus_info" : "VK_EXT_physical_device_drm";
            matches++;
        }
    }
    free(devices);
    return matches == 1;
}

static bool vk_ok(vk_state *v, VkResult result) {
    if (result == VK_ERROR_DEVICE_LOST) v->device_lost = true;
    return result == VK_SUCCESS;
}

static bool find_memory(vk_state *v, uint32_t bits, VkMemoryPropertyFlags required,
                        uint32_t *index) {
    for (uint32_t i = 0; i < v->memory.memoryTypeCount; ++i)
        if ((bits & (1u << i)) &&
            (v->memory.memoryTypes[i].propertyFlags & required) == required) {
            *index = i; return true;
        }
    return false;
}

static bool create_buffer(vk_state *v, VkDeviceSize size, VkBufferUsageFlags usage,
                          VkMemoryPropertyFlags properties, buffer *out) {
    VkBufferCreateInfo info = { .sType = VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO,
        .size = size, .usage = usage, .sharingMode = VK_SHARING_MODE_EXCLUSIVE };
    if (!vk_ok(v, vkCreateBuffer(v->device, &info, NULL, &out->buffer))) return false;
    VkMemoryRequirements req;
    vkGetBufferMemoryRequirements(v->device, out->buffer, &req);
    uint32_t type;
    if (!find_memory(v, req.memoryTypeBits, properties, &type)) return false;
    VkMemoryAllocateInfo alloc = { .sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO,
        .allocationSize = req.size, .memoryTypeIndex = type };
    if (!vk_ok(v, vkAllocateMemory(v->device, &alloc, NULL, &out->memory)) ||
        !vk_ok(v, vkBindBufferMemory(v->device, out->buffer, out->memory, 0))) return false;
    if (properties & VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT)
        if (!vk_ok(v, vkMapMemory(v->device, out->memory, 0, size, 0, &out->mapped))) return false;
    return true;
}

static unsigned char *read_file(const char *path, size_t *size) {
    FILE *handle = fopen(path, "rb");
    if (!handle) return NULL;
    if (fseek(handle, 0, SEEK_END) || (*size = (size_t)ftell(handle)) == 0 || fseek(handle, 0, SEEK_SET)) {
        fclose(handle); return NULL;
    }
    unsigned char *data = malloc(*size);
    if (!data || fread(data, 1, *size, handle) != *size) { free(data); fclose(handle); return NULL; }
    fclose(handle); return data;
}

static const char *default_shader(char path[PATH_MAX]) {
    char exe[PATH_MAX]; ssize_t length = readlink("/proc/self/exe", exe, sizeof(exe) - 1);
    if (length <= 0) return NULL;
    exe[length] = 0; char *slash = strrchr(exe, '/'); if (!slash) return NULL; *slash = 0;
    snprintf(path, PATH_MAX, "%s/shaders/pattern.spv", exe); return path;
}

static bool setup_vulkan(vk_state *v, const options *o) {
    VkApplicationInfo app = { .sType = VK_STRUCTURE_TYPE_APPLICATION_INFO,
        .pApplicationName = "gpu-triage-vram-helper", .applicationVersion = VK_MAKE_VERSION(1,0,0),
        .pEngineName = "none", .engineVersion = 1, .apiVersion = VK_API_VERSION_1_1 };
    VkInstanceCreateInfo instance = { .sType = VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO, .pApplicationInfo = &app };
    if (!vk_ok(v, vkCreateInstance(&instance, NULL, &v->instance))) return false;
    char name[VK_MAX_PHYSICAL_DEVICE_NAME_SIZE] = {0}; const char *source = NULL;
    if (!select_physical(v, o, name, &source)) { emit_identity(o, false, NULL, "no unique exact match"); return false; }
    /* identity is emitted before logical-device creation and every allocation. */
    printf("{\"type\":\"identity\",\"exact_match\":true,\"bdf\":\"%s\","
           "\"vendor_id\":%u,\"device_id\":%u,\"mapping_source\":\"%s\","
           "\"name\":\"%s\"}\n", o->bdf, o->vendor, o->device, source, name);
    fflush(stdout);

    uint32_t families = 0; vkGetPhysicalDeviceQueueFamilyProperties(v->physical, &families, NULL);
    VkQueueFamilyProperties *family = calloc(families, sizeof(*family));
    if (!family) return false;
    vkGetPhysicalDeviceQueueFamilyProperties(v->physical, &families, family);
    bool found = false;
    for (uint32_t i = 0; i < families; ++i)
        if ((family[i].queueFlags & (VK_QUEUE_COMPUTE_BIT | VK_QUEUE_TRANSFER_BIT)) ==
            (VK_QUEUE_COMPUTE_BIT | VK_QUEUE_TRANSFER_BIT)) { v->queue_family = i; found = true; break; }
    free(family); if (!found) return false;
    float priority = 1.0f;
    VkDeviceQueueCreateInfo queue = { .sType = VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO,
        .queueFamilyIndex = v->queue_family, .queueCount = 1, .pQueuePriorities = &priority };
    VkDeviceCreateInfo device = { .sType = VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO,
        .queueCreateInfoCount = 1, .pQueueCreateInfos = &queue };
    if (!vk_ok(v, vkCreateDevice(v->physical, &device, NULL, &v->device))) return false;
    vkGetDeviceQueue(v->device, v->queue_family, 0, &v->queue);
    vkGetPhysicalDeviceMemoryProperties(v->physical, &v->memory);

    uint64_t local_heap = 0;
    for (uint32_t i = 0; i < v->memory.memoryHeapCount; ++i)
        if ((v->memory.memoryHeaps[i].flags & VK_MEMORY_HEAP_DEVICE_LOCAL_BIT) &&
            v->memory.memoryHeaps[i].size > local_heap) local_heap = v->memory.memoryHeaps[i].size;
    uint64_t per_allocation_limit = (local_heap * o->max_vram_percent / 100u) / 2u;
    v->bytes = o->max_bytes < per_allocation_limit ? o->max_bytes : per_allocation_limit;
    VkPhysicalDeviceProperties properties;
    vkGetPhysicalDeviceProperties(v->physical, &properties);
    uint64_t dispatch_limit = (uint64_t)properties.limits.maxComputeWorkGroupCount[0] * 256u * 4u;
    if (v->bytes > properties.limits.maxStorageBufferRange) v->bytes = properties.limits.maxStorageBufferRange;
    if (v->bytes > dispatch_limit) v->bytes = dispatch_limit;
    /* Power-of-two words make every odd stride a complete permutation. */
    uint64_t power_of_two = 4096;
    while (power_of_two <= v->bytes / 2u) power_of_two *= 2u;
    v->bytes = power_of_two;
    if (v->bytes < 4096) return false;
    VkBufferUsageFlags local_usage = VK_BUFFER_USAGE_TRANSFER_SRC_BIT |
        VK_BUFFER_USAGE_TRANSFER_DST_BIT | VK_BUFFER_USAGE_STORAGE_BUFFER_BIT;
    if (!create_buffer(v, v->bytes, VK_BUFFER_USAGE_TRANSFER_SRC_BIT,
                       VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT, &v->host_in) ||
        !create_buffer(v, v->bytes, VK_BUFFER_USAGE_TRANSFER_DST_BIT,
                       VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT, &v->host_out) ||
        !create_buffer(v, v->bytes, local_usage, VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT, &v->local_a) ||
        !create_buffer(v, v->bytes, local_usage, VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT, &v->local_b)) return false;

    VkCommandPoolCreateInfo pool = { .sType = VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO,
        .flags = VK_COMMAND_POOL_CREATE_RESET_COMMAND_BUFFER_BIT, .queueFamilyIndex = v->queue_family };
    if (!vk_ok(v, vkCreateCommandPool(v->device, &pool, NULL, &v->command_pool))) return false;
    VkCommandBufferAllocateInfo command = { .sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO,
        .commandPool = v->command_pool, .level = VK_COMMAND_BUFFER_LEVEL_PRIMARY, .commandBufferCount = 1 };
    if (!vk_ok(v, vkAllocateCommandBuffers(v->device, &command, &v->command))) return false;

    VkDescriptorSetLayoutBinding binding = { .binding = 0, .descriptorType = VK_DESCRIPTOR_TYPE_STORAGE_BUFFER,
        .descriptorCount = 1, .stageFlags = VK_SHADER_STAGE_COMPUTE_BIT };
    VkDescriptorSetLayoutCreateInfo dl = { .sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO,
        .bindingCount = 1, .pBindings = &binding };
    if (!vk_ok(v, vkCreateDescriptorSetLayout(v->device, &dl, NULL, &v->descriptor_layout))) return false;
    VkPushConstantRange range = { .stageFlags = VK_SHADER_STAGE_COMPUTE_BIT, .offset = 0, .size = sizeof(push_params) };
    VkPipelineLayoutCreateInfo pl = { .sType = VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO,
        .setLayoutCount = 1, .pSetLayouts = &v->descriptor_layout,
        .pushConstantRangeCount = 1, .pPushConstantRanges = &range };
    if (!vk_ok(v, vkCreatePipelineLayout(v->device, &pl, NULL, &v->pipeline_layout))) return false;
    VkDescriptorPoolSize pool_size = { .type = VK_DESCRIPTOR_TYPE_STORAGE_BUFFER, .descriptorCount = 1 };
    VkDescriptorPoolCreateInfo dp = { .sType = VK_STRUCTURE_TYPE_DESCRIPTOR_POOL_CREATE_INFO,
        .maxSets = 1, .poolSizeCount = 1, .pPoolSizes = &pool_size };
    if (!vk_ok(v, vkCreateDescriptorPool(v->device, &dp, NULL, &v->descriptor_pool))) return false;
    VkDescriptorSetAllocateInfo ds = { .sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_ALLOCATE_INFO,
        .descriptorPool = v->descriptor_pool, .descriptorSetCount = 1, .pSetLayouts = &v->descriptor_layout };
    if (!vk_ok(v, vkAllocateDescriptorSets(v->device, &ds, &v->descriptor))) return false;

    char shader_path[PATH_MAX]; const char *path = o->shader ? o->shader : default_shader(shader_path);
    size_t shader_size = 0; unsigned char *shader = path ? read_file(path, &shader_size) : NULL;
    if (!shader || shader_size % 4) { free(shader); return false; }
    VkShaderModuleCreateInfo sm = { .sType = VK_STRUCTURE_TYPE_SHADER_MODULE_CREATE_INFO,
        .codeSize = shader_size, .pCode = (const uint32_t *)shader };
    bool shader_ok = vk_ok(v, vkCreateShaderModule(v->device, &sm, NULL, &v->shader_module));
    free(shader); if (!shader_ok) return false;
    VkPipelineShaderStageCreateInfo stage = { .sType = VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO,
        .stage = VK_SHADER_STAGE_COMPUTE_BIT, .module = v->shader_module, .pName = "main" };
    VkComputePipelineCreateInfo cp = { .sType = VK_STRUCTURE_TYPE_COMPUTE_PIPELINE_CREATE_INFO,
        .stage = stage, .layout = v->pipeline_layout };
    return vk_ok(v, vkCreateComputePipelines(v->device, VK_NULL_HANDLE, 1, &cp, NULL, &v->pipeline));
}

static void destroy_buffer(VkDevice device, buffer *b) {
    if (b->mapped) vkUnmapMemory(device, b->memory);
    if (b->buffer) vkDestroyBuffer(device, b->buffer, NULL);
    if (b->memory) vkFreeMemory(device, b->memory, NULL);
}

static void cleanup(vk_state *v) {
    if (v->device) {
        if (!v->device_lost) (void)vkDeviceWaitIdle(v->device);
        destroy_buffer(v->device, &v->host_in); destroy_buffer(v->device, &v->host_out);
        destroy_buffer(v->device, &v->local_a); destroy_buffer(v->device, &v->local_b);
        if (v->pipeline) vkDestroyPipeline(v->device, v->pipeline, NULL);
        if (v->shader_module) vkDestroyShaderModule(v->device, v->shader_module, NULL);
        if (v->pipeline_layout) vkDestroyPipelineLayout(v->device, v->pipeline_layout, NULL);
        if (v->descriptor_pool) vkDestroyDescriptorPool(v->device, v->descriptor_pool, NULL);
        if (v->descriptor_layout) vkDestroyDescriptorSetLayout(v->device, v->descriptor_layout, NULL);
        if (v->command_pool) vkDestroyCommandPool(v->device, v->command_pool, NULL);
        vkDestroyDevice(v->device, NULL);
    }
    if (v->instance) vkDestroyInstance(v->instance, NULL);
}

static bool begin(vk_state *v) {
    if (!vk_ok(v, vkResetCommandBuffer(v->command, 0))) return false;
    VkCommandBufferBeginInfo info = { .sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO,
        .flags = VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT };
    return vk_ok(v, vkBeginCommandBuffer(v->command, &info));
}

static void barrier(vk_state *v, VkPipelineStageFlags src_stage, VkAccessFlags src_access,
                    VkPipelineStageFlags dst_stage, VkAccessFlags dst_access) {
    VkMemoryBarrier memory = { .sType = VK_STRUCTURE_TYPE_MEMORY_BARRIER,
        .srcAccessMask = src_access, .dstAccessMask = dst_access };
    vkCmdPipelineBarrier(v->command, src_stage, dst_stage, 0, 1, &memory, 0, NULL, 0, NULL);
}

static bool submit(vk_state *v) {
    if (!vk_ok(v, vkEndCommandBuffer(v->command))) return false;
    VkSubmitInfo submit = { .sType = VK_STRUCTURE_TYPE_SUBMIT_INFO,
        .commandBufferCount = 1, .pCommandBuffers = &v->command };
    return vk_ok(v, vkQueueSubmit(v->queue, 1, &submit, VK_NULL_HANDLE)) &&
           vk_ok(v, vkQueueWaitIdle(v->queue));
}

static bool transfer_roundtrip(vk_state *v, bool local_copy) {
    if (!begin(v)) return false;
    VkBufferCopy region = { .size = v->bytes };
    vkCmdCopyBuffer(v->command, v->host_in.buffer, v->local_a.buffer, 1, &region);
    barrier(v, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_ACCESS_TRANSFER_WRITE_BIT,
            VK_PIPELINE_STAGE_TRANSFER_BIT, VK_ACCESS_TRANSFER_READ_BIT);
    VkBuffer source = v->local_a.buffer;
    if (local_copy) {
        vkCmdCopyBuffer(v->command, v->local_a.buffer, v->local_b.buffer, 1, &region);
        barrier(v, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_ACCESS_TRANSFER_WRITE_BIT,
                VK_PIPELINE_STAGE_TRANSFER_BIT, VK_ACCESS_TRANSFER_READ_BIT);
        source = v->local_b.buffer;
    }
    vkCmdCopyBuffer(v->command, source, v->host_out.buffer, 1, &region);
    return submit(v);
}

static bool compute_roundtrip(vk_state *v, buffer *target, const push_params *params) {
    VkDescriptorBufferInfo bi = { .buffer = target->buffer, .offset = 0, .range = v->bytes };
    VkWriteDescriptorSet write = { .sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET,
        .dstSet = v->descriptor, .dstBinding = 0, .descriptorCount = 1,
        .descriptorType = VK_DESCRIPTOR_TYPE_STORAGE_BUFFER, .pBufferInfo = &bi };
    vkUpdateDescriptorSets(v->device, 1, &write, 0, NULL);
    if (!begin(v)) return false;
    vkCmdBindPipeline(v->command, VK_PIPELINE_BIND_POINT_COMPUTE, v->pipeline);
    vkCmdBindDescriptorSets(v->command, VK_PIPELINE_BIND_POINT_COMPUTE, v->pipeline_layout,
                            0, 1, &v->descriptor, 0, NULL);
    vkCmdPushConstants(v->command, v->pipeline_layout, VK_SHADER_STAGE_COMPUTE_BIT,
                       0, sizeof(*params), params);
    vkCmdDispatch(v->command, (params->words + 255u) / 256u, 1, 1);
    barrier(v, VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT, VK_ACCESS_SHADER_WRITE_BIT,
            VK_PIPELINE_STAGE_TRANSFER_BIT, VK_ACCESS_TRANSFER_READ_BIT);
    VkBufferCopy copy = { .size = v->bytes };
    vkCmdCopyBuffer(v->command, target->buffer, v->host_out.buffer, 1, &copy);
    return submit(v);
}

static int64_t read_temperature(const options *o) {
    char root[PATH_MAX]; snprintf(root, sizeof(root), "/sys/bus/pci/devices/%s/hwmon", o->bdf);
    DIR *dir = opendir(root); if (!dir) return -1;
    struct dirent *entry; int64_t maximum = -1;
    while ((entry = readdir(dir))) {
        if (strncmp(entry->d_name, "hwmon", 5)) continue;
        char hwmon[PATH_MAX]; snprintf(hwmon, sizeof(hwmon), "%s/%s", root, entry->d_name);
        DIR *sensors = opendir(hwmon); if (!sensors) continue;
        struct dirent *sensor;
        while ((sensor = readdir(sensors))) {
            if (strncmp(sensor->d_name, "temp", 4) || !strstr(sensor->d_name, "_input")) continue;
            char path[PATH_MAX]; snprintf(path, sizeof(path), "%s/%s", hwmon, sensor->d_name);
            FILE *file = fopen(path, "r"); long long value;
            if (file && fscanf(file, "%lld", &value) == 1 && value > maximum) maximum = value;
            if (file) fclose(file);
        }
        closedir(sensors);
    }
    closedir(dir); return maximum;
}

static void compare_words(const options *o, gt_error_summary *summary, experiment *e,
                          const uint32_t *expected, const uint32_t *actual, size_t words,
                          uint32_t allocation, const char *pattern, uint32_t seed,
                          uint32_t pass, uint32_t reread, int64_t temp) {
    e->comparisons += words;
    for (size_t i = 0; i < words; ++i) if (expected[i] != actual[i]) {
        e->errors++;
        emit_error(summary, o, allocation, i * 4u, expected[i], actual[i],
                   pattern, seed, pass, reread, temp);
    }
}

static void unavailable_experiments(experiment e[4], const char *status) {
    for (size_t i = 0; i < 4; ++i) e[i] = (experiment){ .status = status };
}

static int self_test(const options *o) {
    gt_error_summary summary; gt_summary_init(&summary);
    experiment e[4] = {{0,0,"PASS"},{0,0,"PASS"},{0,0,"PASS"},{0,0,"FAIL"}};
    uint32_t expected[128], actual[128];
    for (uint32_t pattern = 0; pattern < GT_PATTERN_COUNT; ++pattern) {
        gt_fill_pattern(expected, ARRAY_LEN(expected), pattern, 1234u + pattern, 17);
        memset(actual, 0, sizeof(actual));
        for (size_t logical = 0; logical < ARRAY_LEN(actual); ++logical) {
            size_t physical = logical * 17u % ARRAY_LEN(actual);
            actual[physical] = gt_pattern_value(pattern, (uint32_t)logical, 1234u + pattern);
        }
        if (memcmp(expected, actual, sizeof(expected))) return 2;
    }
    memcpy(actual, expected, sizeof(actual)); actual[16] ^= 0x00020000u; actual[32] ^= 1u;
    compare_words(o, &summary, &e[3], expected, actual, ARRAY_LEN(expected), 0,
                  "deterministic_prng", 1242, 0, 0, -1);
    compare_words(o, &summary, &e[3], expected, actual, ARRAY_LEN(expected), 0,
                  "deterministic_prng", 1242, 1, 1, -1);
    compare_words(o, &summary, &e[3], expected, actual, ARRAY_LEN(expected), 1,
                  "deterministic_prng", 1242, 2, 0, -1);
    emit_identity(o, true, "VK_EXT_pci_bus_info", "synthetic self-test device");
    for (size_t i = 0; i < 4; ++i) emit_experiment((const char *[]){"host_transfer","gpu_local_copy","compute_kat","vram_pattern"}[i], &e[i]);
    emit_summary(o, e, &summary, sizeof(expected), "UNAVAILABLE", -1, false);
    return summary.total == 6 && gt_reproducible_reread(&summary) == 2 &&
           gt_reproducible_pass(&summary) == 2 && gt_reproducible_allocation(&summary) == 2 ? 0 : 2;
}

static int run_hardware(const options *o) {
    vk_state v = {0}; gt_error_summary summary; gt_summary_init(&summary);
    experiment e[4]; unavailable_experiments(e, "UNAVAILABLE");
    int64_t max_temp = -1; const char *temp_status = "UNAVAILABLE";
    if (!setup_vulkan(&v, o)) {
        for (size_t i = 0; i < 4; ++i) emit_experiment((const char *[]){"host_transfer","gpu_local_copy","compute_kat","vram_pattern"}[i], &e[i]);
        emit_summary(o, e, &summary, o->max_bytes, temp_status, max_temp, v.device_lost);
        cleanup(&v); return 2;
    }
    size_t words = (size_t)(v.bytes / 4u); uint32_t *input = v.host_in.mapped, *output = v.host_out.mapped;
    int64_t temp = read_temperature(o); if (temp >= 0) { max_temp = temp; temp_status = "PASS"; }

    gt_fill_pattern(input, words, 8, 0x13579bdfu, 1);
    e[0] = (experiment){ .status = "PASS" };
    if (!transfer_roundtrip(&v, false)) e[0].status = "INCONCLUSIVE";
    else compare_words(o, &summary, &e[0], input, output, words, 0, "host_transfer_prng", 0x13579bdfu, 0, 0, temp);
    if (e[0].errors) e[0].status = "FAIL";
    emit_experiment("host_transfer", &e[0]);

    e[1] = (experiment){ .status = "PASS" };
    if (!v.device_lost && !transfer_roundtrip(&v, true)) e[1].status = "INCONCLUSIVE";
    else if (!v.device_lost) compare_words(o, &summary, &e[1], input, output, words, 1, "gpu_local_copy", 0x13579bdfu, 0, 0, temp);
    if (e[1].errors) e[1].status = "FAIL";
    if (v.device_lost) e[1].status = "INCONCLUSIVE";
    emit_experiment("gpu_local_copy", &e[1]);

    e[2] = (experiment){ .status = "PASS" };
    push_params params = { .words = (uint32_t)words, .pattern = 9, .seed = 0x2468ace0u, .stride = 1 };
    gt_fill_pattern(input, words, 9, params.seed, 1);
    if (!v.device_lost && !compute_roundtrip(&v, &v.local_a, &params)) e[2].status = "INCONCLUSIVE";
    else if (!v.device_lost) compare_words(o, &summary, &e[2], input, output, words, 0, "compute_kat", params.seed, 0, 0, temp);
    if (e[2].errors) e[2].status = "FAIL";
    if (v.device_lost) e[2].status = "INCONCLUSIVE";
    emit_experiment("compute_kat", &e[2]);

    e[3] = (experiment){ .status = "PASS" };
    const uint32_t strides[] = {1, 17, 65, 257};
    uint32_t pass = 0;
    bool time_limit_reached = false;
    for (uint32_t allocation = 0; allocation < 2 && !v.device_lost; ++allocation) {
        buffer *local = allocation ? &v.local_b : &v.local_a;
        for (uint32_t pattern = 0; pattern < GT_PATTERN_COUNT && !v.device_lost; ++pattern) {
            uint32_t variants = pattern == 4 || pattern == 5 ? 32 : 1;
            for (uint32_t variant = 0; variant < variants && !v.device_lost; ++variant) {
              for (uint32_t repeat = 0; repeat < 2 && !v.device_lost; ++repeat) {
                if (monotonic_ms() - start_ms >= o->seconds * 1000u) {
                    time_limit_reached = true; e[3].status = "INCONCLUSIVE"; goto patterns_done;
                }
                temp = read_temperature(o);
                if (temp >= 0) { temp_status = "PASS"; if (temp > max_temp) max_temp = temp; }
                if (o->max_temp_mc >= 0 && temp >= o->max_temp_mc) {
                    temp_status = "LIMIT_REACHED"; e[3].status = "INCONCLUSIVE"; goto patterns_done;
                }
                uint32_t seed = (pattern == 4 || pattern == 5) ? variant : 0xc001d00du + pattern;
                uint32_t stride = strides[(pattern + variant) % ARRAY_LEN(strides)];
                gt_fill_pattern(input, words, pattern, seed, stride);
                params = (push_params){ .words=(uint32_t)words, .pattern=pattern, .seed=seed, .stride=stride };
                if (!compute_roundtrip(&v, local, &params)) { e[3].status = "INCONCLUSIVE"; goto patterns_done; }
                compare_words(o, &summary, &e[3], input, output, words, allocation,
                              gt_pattern_name(pattern), seed, pass, 0, temp);
                /* A second transfer-only read verifies persistence without rewriting. */
                if (!begin(&v)) { e[3].status = "INCONCLUSIVE"; goto patterns_done; }
                VkBufferCopy copy = { .size = v.bytes };
                vkCmdCopyBuffer(v.command, local->buffer, v.host_out.buffer, 1, &copy);
                if (!submit(&v)) { e[3].status = "INCONCLUSIVE"; goto patterns_done; }
                compare_words(o, &summary, &e[3], input, output, words, allocation,
                              gt_pattern_name(pattern), seed, pass, 1, temp);
                pass++;
              }
            }
        }
    }
patterns_done:
    if (e[3].errors) e[3].status = "FAIL";
    if (v.device_lost) e[3].status = "INCONCLUSIVE";
    if (time_limit_reached && !e[3].errors) e[3].status = "INCONCLUSIVE";
    emit_experiment("vram_pattern", &e[3]);
    emit_summary(o, e, &summary, v.bytes, temp_status, max_temp, v.device_lost);
    int result = v.device_lost ? 2 : summary.total ? 1 :
        (!strcmp(e[3].status, "PASS") ? 0 : 2);
    cleanup(&v); return result;
}

int main(int argc, char **argv) {
    options o;
    if (!parse_options(argc, argv, &o)) {
        fprintf(stderr, "usage: %s --gpu DDDD:BB:DD.F --vendor ID --device ID --seconds N "
                "--max-bytes N --max-errors N [--max-vram-percent N] [--max-temp-mc N]\n", argv[0]);
        return 2;
    }
    start_ms = monotonic_ms(); emit_meta(&o);
    return o.self_test ? self_test(&o) : run_hardware(&o);
}
