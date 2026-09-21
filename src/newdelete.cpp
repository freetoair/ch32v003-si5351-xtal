// Minimal bump allocator for operator new / delete.
//
// Nothing in the firmware allocates any more: this was needed only by the
// Etherkit Si5351 library, which does `new uint8_t[20]` inside set_ms() and
// set_pll(). The library is no longer used (see platformio.ini), so the linker
// discards all of this. It is kept so that restoring the library — which
// platformio.ini documents how to do — still links on a chip with 2 KB of RAM
// and no heap.

#include <cstddef>
#include <cstdint>

static uint8_t new_pool[256];
static size_t new_pool_used = 0;

void* operator new[](size_t size) {
    void* p = &new_pool[new_pool_used];
    new_pool_used += size;
    return p;
}
void operator delete[](void* p) noexcept { new_pool_used = 0; }
void operator delete[](void* p, size_t size) noexcept { (void)size; new_pool_used = 0; }

void* operator new(size_t size) {
    void* p = &new_pool[new_pool_used];
    new_pool_used += size;
    return p;
}
void operator delete(void* p) noexcept { new_pool_used = 0; }
void operator delete(void* p, size_t size) noexcept { (void)size; new_pool_used = 0; }
