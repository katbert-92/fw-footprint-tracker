#!/bin/sh
# Rebuilds v1/ and v2/ with the Arm GNU toolchain. Each version links from its
# own directory so both maps name the same objects, and the prefix map keeps
# this machine's paths out of the DWARF.
set -eu
cd "$(dirname "$0")"

for v in v1 v2; do
    rm -rf "$v" && mkdir "$v"
    def=""
    [ "$v" = v2 ] && def="-DV2"
    (
        cd "$v"
        flags="-mcpu=cortex-m4 -mthumb -Os -g -ffunction-sections -fdata-sections -fdebug-prefix-map=$(cd .. && pwd)=."
        arm-none-eabi-gcc $flags $def -I.. -c ../util.c -o util.o
        arm-none-eabi-ar rcs libutil.a util.o
        arm-none-eabi-gcc $flags $def -I.. -c ../main.c -o main.o
        arm-none-eabi-gcc -mcpu=cortex-m4 -mthumb -nostdlib -Wl,--gc-sections \
            -Wl,-Map=fw.map -T ../link.ld main.o libutil.a -o fw.elf
        rm -f util.o main.o libutil.a
    )
done
