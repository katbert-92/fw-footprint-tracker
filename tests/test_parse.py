from fwtrack.elf_map_parse import ElfParser


def test_toolchain_is_read_from_comment(v1):
    assert ElfParser(v1[0]).get_toolchain().startswith("GCC: ")
