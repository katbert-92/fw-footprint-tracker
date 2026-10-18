#include "util.h"

#ifdef V2
static const int weights[16] = {3, 1, 4, 1, 5, 9, 2, 6, 5, 3, 5, 8, 9, 7, 9, 3};
#endif

int util_sum(const int *v, int n)
{
    int s = 0;
    for (int i = 0; i < n; i++) {
#ifdef V2
        s += v[i] * weights[i & 15];
#else
        s += v[i];
#endif
    }
    return s;
}

#ifdef V2
/* The same literal as in main.c: ld keeps one copy for both objects. */
const char *util_name(void)
{
    return "a string literal has no symbol of its own";
}
#endif

int util_unused(int x)
{
    return x + 1;
}
