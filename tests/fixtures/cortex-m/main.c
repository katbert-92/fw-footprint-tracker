#include "util.h"

void Reset_Handler(void);

__attribute__((section(".isr_vector"), used))
static void (*const vectors[])(void) = {(void (*)(void))0x20004000, Reset_Handler};

static int counter;
int table[8] = {1, 2, 3, 4, 5, 6, 7, 8};
const char banner[] = "fixture";
__attribute__((section(".backup"))) int backup_word;
const char *volatile sink;

static int __attribute__((noinline)) helper(int x)
{
    return x * 3 + counter;
}

#ifdef V2
int __attribute__((noinline)) extra_feature(int x)
{
    int acc = 0;
    for (int i = 0; i < x; i++) {
        acc ^= (acc << 3) + i * 7 + table[i & 7];
    }
    return acc;
}
#endif

void Reset_Handler(void)
{
    for (;;) {
        counter = util_sum(table, 8) + helper(counter);
        backup_word = counter;
#ifdef V2
        counter += extra_feature(counter);
        sink = "a string literal has no symbol of its own";
        sink = util_name();
#endif
    }
}
