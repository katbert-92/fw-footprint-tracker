from fwtrack.analyse import compute_memory_usage
from fwtrack.elf_map_parse import ElfParser, MapParser


def test_regions_without_attributes_are_kept(v1):
    regions = MapParser(v1[1]).get_regions_info()

    assert regions == {
        "FLASH": {"origin": 0x08000000, "length": 64 * 1024},
        "RAM": {"origin": 0x20000000, "length": 16 * 1024},
        # Declared without attributes, which ld prints as nothing at all.
        "BACKUP": {"origin": 0x40024000, "length": 4 * 1024},
    }


def test_input_sections_cover_every_allocated_section(v1):
    sections = ElfParser(v1[0]).get_allocated_sections()
    inputs = MapParser(v1[1]).get_input_sections()

    for section in sections:
        placed = sum(i["size"] for i in inputs if i["output"] == section["name"])
        assert placed == section["size"], section["name"]


def test_input_sections_name_their_objects(v2):
    inputs = {i["name"]: i for i in MapParser(v2[1]).get_input_sections()}

    # A name too long for its column puts the address on the next line.
    assert inputs[".text.extra_feature"]["object"] == "main.o"
    assert inputs[".text.extra_feature"]["size"] == 0x2C
    assert inputs[".text.util_sum"]["object"] == "libutil.a(util.o)"
    assert inputs["*fill*"]["object"] is None


def test_data_is_charged_to_flash_and_ram(v1):
    regions = MapParser(v1[1]).get_regions_info()
    usage = compute_memory_usage(regions, ElfParser(v1[0]).get_segments_info())

    data = next(s for s in ElfParser(v1[0]).get_allocated_sections() if s["name"] == ".data")
    assert data["lma"] != data["vma"]
    # .data runs from RAM, and its initialiser is the last thing in flash.
    assert usage["FLASH"]["used"] == data["lma"] + data["size"] - regions["FLASH"]["origin"]
    assert usage["RAM"]["used"] >= data["size"]
    assert usage["BACKUP"]["used"] == 4


def test_thumb_bit_is_cleared(v1):
    symbols = {s["name"]: s for s in ElfParser(v1[0]).get_symbols()}
    text = next(s for s in ElfParser(v1[0]).get_allocated_sections() if s["name"] == ".text")

    handler = symbols["Reset_Handler"]
    assert handler["code"] and handler["addr"] % 2 == 0
    assert text["vma"] <= handler["addr"] < text["vma"] + text["size"]


def test_dwarf_names_the_source_of_code_and_data(v2):
    elf = ElfParser(v2[0])
    lookup = elf.get_source_lookup()
    symbols = {s["name"]: s for s in elf.get_symbols()}

    assert lookup.code(symbols["util_sum"]["addr"]).endswith("util.c")
    assert lookup.data[symbols["table"]["addr"]].endswith("main.c")


def test_toolchain_is_read_from_comment(v1):
    assert ElfParser(v1[0]).get_toolchain().startswith("GCC: ")


def test_size_before_relaxing_is_kept(tmp_path):
    map_file = tmp_path / "fw.map"
    map_file.write_text(
        "Memory Configuration\n\n"
        "Name             Origin             Length             Attributes\n"
        "FLASH            0x08000000         0x00010000         xr\n"
        "*default*        0x00000000         0xffffffff\n\n"
        "Linker script and memory map\n\n"
        ".rodata         0x08000000       0x30\n"
        " .rodata.str1.4\n"
        "                0x08000000       0x30 a.o\n"
        "                                 0x20 (size before relaxing)\n"
        " .rodata.str1.4\n"
        "                0x08000030        0x0 b.o\n"
        "                                 0x18 (size before relaxing)\n"
    )

    inputs = MapParser(map_file).get_input_sections()

    assert [(i["object"], i["size"], i.get("raw")) for i in inputs] == [
        ("a.o", 0x30, 0x20),
        ("b.o", 0, 0x18),
    ]
