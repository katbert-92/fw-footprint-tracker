from fwtrack.elf_map_parse import ElfParser, MapParser


def test_regions_without_attributes_are_kept(v1):
    regions = MapParser(v1[1]).get_regions_info()

    assert regions == {
        "FLASH": {"origin": 0x08000000, "length": 64 * 1024},
        "RAM": {"origin": 0x20000000, "length": 16 * 1024},
        # Declared without attributes, which ld prints as nothing at all.
        "BACKUP": {"origin": 0x40024000, "length": 4 * 1024},
    }


def test_toolchain_is_read_from_comment(v1):
    assert ElfParser(v1[0]).get_toolchain().startswith("GCC: ")
