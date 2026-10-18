/* BACKUP has no attributes on purpose: ld then prints no attribute column for it. */
MEMORY
{
  FLASH (rx)  : ORIGIN = 0x08000000, LENGTH = 64K
  RAM   (rwx) : ORIGIN = 0x20000000, LENGTH = 16K
  BACKUP      : ORIGIN = 0x40024000, LENGTH = 4K
}

ENTRY(Reset_Handler)

SECTIONS
{
  .isr_vector : { KEEP(*(.isr_vector)) } > FLASH
  .text       : { *(.text .text.*) } > FLASH
  .rodata     : { *(.rodata .rodata.*) } > FLASH
  .data       : { *(.data .data.*) } > RAM AT > FLASH
  .bss (NOLOAD)    : { *(.bss .bss.* COMMON) } > RAM
  .backup (NOLOAD) : { *(.backup) } > BACKUP
}
