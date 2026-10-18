"""Parsers for the two artefacts a GNU toolchain link produces.

MapParser reads the MEMORY block of a linker map file, which is where the
physical size of every region comes from -- nothing needs to be configured for
that -- and the input sections, which say which object every byte came from.
ElfParser reads segments, sections, symbols and the DWARF line tables.
"""

import bisect
import io
import os
import re
from pathlib import Path

from elftools.elf.constants import SH_FLAGS
from elftools.elf.elffile import ELFFile

from .log import get_logger

logger = get_logger(__name__)

# The attribute column is optional: ld prints nothing after the length of a
# region declared without one, and requiring it used to drop that region and
# every region listed after it.
REGION_RE = re.compile(r"^(\S+)\s+0x([0-9a-fA-F]+)\s+0x([0-9a-fA-F]+)(?:\s+.*)?$")

INPUT_RE = re.compile(r"^ (\S+)\s+0x([0-9a-fA-F]+)\s+0x([0-9a-fA-F]+)\s+(\S.*?)\s*$")
INPUT_NAME_RE = re.compile(r"^ (\S+)\s*$")
INPUT_TAIL_RE = re.compile(r"^\s+0x([0-9a-fA-F]+)\s+0x([0-9a-fA-F]+)\s+(\S.*?)\s*$")
FILL_RE = re.compile(r"^ \*fill\*\s+0x([0-9a-fA-F]+)\s+0x([0-9a-fA-F]+)")
RAW_SIZE_RE = re.compile(r"^\s+0x([0-9a-fA-F]+) \(size before relaxing\)\s*$")

# Lines at column 0 of the memory map that are not output sections.
MAP_KEYWORDS = ("LOAD ", "OUTPUT(", "START GROUP", "END GROUP", "INSERT ")

FILL = "*fill*"


class MapParser:
    def __init__(self, map_file_path: Path) -> None:
        self.map_file_path = map_file_path
        self.map_lines = []
        self._load_map_data()

    def _load_map_data(self) -> None:
        logger.debug(f"Loading map file: {self.map_file_path}")
        try:
            with open(self.map_file_path, encoding="utf-8", errors="replace") as f:
                self.map_lines = f.read().splitlines()
        except OSError as e:
            # Raised, not exited: this runs inside someone else's build system,
            # and SystemExit walks straight through an `except Exception` that
            # was meant to keep a footprint failure from stopping a build.
            raise OSError(f"Cannot read the map file {self.map_file_path}: {e}") from e

    def get_regions_info(self) -> dict:
        """Physical memory regions: name, origin and length as the linker sees them."""
        header_index = None
        for i, line in enumerate(self.map_lines):
            if line.strip().startswith("Name") and "Origin" in line and "Length" in line:
                header_index = i
                break

        if header_index is None:
            logger.warning(f"No memory configuration in {self.map_file_path}")
            return {}

        memory_regions = {}
        for line in self.map_lines[header_index + 1 :]:
            if line.strip() == "" or line.strip().startswith("*default*"):
                continue

            match = REGION_RE.match(line)
            if not match:
                break

            name, origin, length = match.groups()
            memory_regions[name] = {"origin": int(origin, 16), "length": int(length, 16)}

        logger.debug(f"Parsed {len(memory_regions)} memory regions")
        return memory_regions

    def get_input_sections(self) -> list:
        """Every input section placed in the image, with the object it came from.

        Read from the memory map rather than the ELF: the ELF knows the output
        sections, and only the map says which object file each part of them
        came from. An input section whose name does not fit beside its address
        is printed on a line of its own, with the address on the next one.

        "raw" is the size before the linker shrank the section, by relaxing
        code or by merging strings into another object's copy.
        """
        start = next(
            (
                i
                for i, line in enumerate(self.map_lines)
                if line.startswith("Linker script and memory map")
            ),
            None,
        )
        if start is None:
            return []

        sections = []
        output = None
        pending = None
        for line in self.map_lines[start + 1 :]:
            if not line.strip():
                continue
            if line.startswith("Cross Reference Table"):
                break

            if not line[0].isspace():
                pending = None
                if not line.startswith(MAP_KEYWORDS):
                    output = line.split()[0]
                continue

            raw = RAW_SIZE_RE.match(line)
            if raw:
                if sections:
                    sections[-1]["raw"] = int(raw.group(1), 16)
                continue

            fill = FILL_RE.match(line)
            if fill:
                sections.append(_input(output, FILL, fill.group(1), fill.group(2), None))
                continue
            if line.startswith(" *"):
                continue

            full = INPUT_RE.match(line)
            if full:
                sections.append(_input(output, *full.groups()))
                pending = None
                continue

            alone = INPUT_NAME_RE.match(line)
            if alone:
                pending = alone.group(1)
                continue

            tail = INPUT_TAIL_RE.match(line)
            if tail and pending:
                sections.append(_input(output, pending, *tail.groups()))
            pending = None

        return sections


def _input(output: str | None, name: str, addr: str, size: str, obj: str | None) -> dict:
    return {
        "output": output,
        "name": name,
        "addr": int(addr, 16),
        "size": int(size, 16),
        "object": obj,
    }


class ElfParser:
    def __init__(self, elf_path: Path) -> None:
        self.elf_path = elf_path
        self.elf_data = None
        self.elf = None
        self._load_elf_data()

    def _load_elf_data(self) -> None:
        logger.debug(f"Loading ELF file: {self.elf_path}")
        try:
            with open(self.elf_path, "rb") as f:
                self.elf_data = f.read()
            self.elf = ELFFile(io.BytesIO(self.elf_data))
        except OSError as e:
            raise OSError(f"Cannot read the ELF file {self.elf_path}: {e}") from e

    def get_sections_info(self) -> dict:
        return {section.name: dict(section.header) for section in self.elf.iter_sections()}

    def get_segments_info(self) -> list:
        """Loadable segments, the authoritative view of what the image occupies.

        p_paddr/p_filesz say where the bytes live in the image (flash), while
        p_vaddr/p_memsz say what is occupied while running (RAM). For a plain
        flash-resident segment the two coincide; for .data they differ, which is
        exactly how an initialiser gets charged to flash without anyone having
        to name the flash region.
        """
        segments = [
            {
                "vaddr": s.header.p_vaddr,
                "paddr": s.header.p_paddr,
                "filesz": s.header.p_filesz,
                "memsz": s.header.p_memsz,
            }
            for s in self.elf.iter_segments()
            if s.header.p_type == "PT_LOAD"
        ]

        logger.debug(f"Found {len(segments)} loadable segments")
        return segments

    def get_toolchain(self) -> str:
        """Compiler identification from .comment, when the toolchain emits it."""
        section = self.elf.get_section_by_name(".comment")
        if section is None:
            return ""

        entries = [e for e in section.data().decode("utf-8", "replace").split("\0") if e]
        return entries[0] if entries else ""

    def get_allocated_sections(self) -> list:
        """Sections that occupy memory, with the address they are loaded from.

        The load address comes from the segment holding the section: .data runs
        from RAM but its initialiser is stored in flash, and only the program
        headers say where.
        """
        segments = self.get_segments_info()
        sections = []
        for index, section in enumerate(self.elf.iter_sections()):
            header = section.header
            if not header.sh_flags & SH_FLAGS.SHF_ALLOC or not header.sh_size:
                continue

            vma = header.sh_addr
            lma = vma
            for seg in segments:
                if seg["vaddr"] <= vma < seg["vaddr"] + max(seg["memsz"], 1):
                    lma = vma - seg["vaddr"] + seg["paddr"]
                    break

            sections.append(
                {
                    "index": index,
                    "name": section.name,
                    "vma": vma,
                    "lma": lma,
                    "size": header.sh_size,
                    # NOBITS takes room only while running: nothing is stored.
                    "stored": header.sh_type != "SHT_NOBITS",
                }
            )
        return sections

    def get_symbols(self) -> list:
        """Sized functions and objects, each with the section it lives in.

        On Arm the low bit of a function address only selects Thumb mode, and
        has to be cleared before the address can be compared with anything.
        """
        symtab = self.elf.get_section_by_name(".symtab")
        if symtab is None:
            return []

        thumb = self.elf["e_machine"] == "EM_ARM"
        current_file = None
        symbols = []
        for symbol in symtab.iter_symbols():
            entry = symbol.entry
            kind = entry["st_info"]["type"]
            if kind == "STT_FILE":
                # Locals follow the FILE entry of their translation unit, which
                # is the one hint at their origin an ELF without DWARF still has.
                current_file = symbol.name
                continue
            if kind not in ("STT_FUNC", "STT_OBJECT", "STT_NOTYPE") or entry["st_size"] <= 0:
                continue
            if not isinstance(entry["st_shndx"], int) or not symbol.name:
                continue

            addr = entry["st_value"]
            if thumb and kind == "STT_FUNC":
                addr &= ~1

            bind = entry["st_info"]["bind"]
            symbols.append(
                {
                    "name": symbol.name,
                    "addr": addr,
                    "size": entry["st_size"],
                    "section": entry["st_shndx"],
                    "code": kind == "STT_FUNC",
                    "global": bind != "STB_LOCAL",
                    "file": current_file if bind == "STB_LOCAL" else None,
                }
            )
        return symbols

    def get_source_lookup(self):
        """Source file of an address, from DWARF; None without debug info.

        Code is found through the line tables, data through the variables each
        compilation unit declares. Both survive LTO, which leaves the map with
        nothing but a temporary object to attribute everything to.
        """
        if not self.elf.has_dwarf_info():
            return None

        try:
            return _SourceLookup(self.elf)
        except Exception as e:
            # Malformed or unusual DWARF must not take the size report with it.
            logger.warning(f"Ignoring the DWARF of {self.elf_path}: {e}")
            return None


class _SourceLookup:
    def __init__(self, elf: ELFFile) -> None:
        self._dwarf = elf.get_dwarf_info()
        self._tables = {}
        little = elf.little_endian
        code = []
        self.data = {}

        for cu in self._dwarf.iter_CUs():
            program, files = self._files(cu)
            if program:
                last = None
                for entry in program.get_entries():
                    state = entry.state
                    if state is None or state.end_sequence:
                        last = None
                        continue
                    if state.file != last and state.file in files:
                        code.append((state.address, files[state.file]))
                        last = state.file

            for die in cu.get_top_DIE().iter_children():
                if die.tag != "DW_TAG_variable":
                    continue
                addr = _static_address(die, cu["address_size"], little)
                if addr is None:
                    continue
                path = self._decl_path(die)
                if path:
                    self.data[addr] = path

        code.sort()
        self._addrs = [a for a, _ in code]
        self._paths = [p for _, p in code]

    def code(self, addr: int) -> str | None:
        i = bisect.bisect_right(self._addrs, addr) - 1
        return self._paths[i] if i >= 0 else None

    def _files(self, cu) -> tuple:
        if cu.cu_offset not in self._tables:
            program = self._dwarf.line_program_for_CU(cu)
            comp_dir = _attr_str(cu.get_top_DIE(), "DW_AT_comp_dir")
            files = _file_table(program, comp_dir) if program else {}
            self._tables[cu.cu_offset] = (program, files)
        return self._tables[cu.cu_offset]

    def _decl_path(self, die) -> str | None:
        """Where a variable was declared, following LTO's indirection if needed.

        Under LTO the variable that has the address is a stub pointing at the
        declaration in the original compilation unit, whose file table is the
        one its decl_file indexes.
        """
        for _ in range(3):
            decl = die.attributes.get("DW_AT_decl_file")
            files = self._files(die.cu)[1]
            if decl is not None and decl.value in files:
                return files[decl.value]

            origin = next(
                (
                    n
                    for n in ("DW_AT_specification", "DW_AT_abstract_origin")
                    if n in die.attributes
                ),
                None,
            )
            if origin is None:
                return None
            die = die.get_DIE_from_attribute(origin)

        return None


def _attr_str(die, name: str) -> str:
    attr = die.attributes.get(name)
    if attr is None:
        return ""
    value = attr.value
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)


def _file_table(program, comp_dir: str) -> dict:
    """File index -> path, the way the line program numbers its files."""
    version = program.header.version
    dirs = [
        d.decode("utf-8", "replace") if isinstance(d, bytes) else d
        for d in program["include_directory"]
    ]
    table = {}
    for i, entry in enumerate(program["file_entry"]):
        name = (
            entry.name.decode("utf-8", "replace") if isinstance(entry.name, bytes) else entry.name
        )
        if version >= 5:
            directory = dirs[entry.dir_index] if entry.dir_index < len(dirs) else ""
            index = i
        else:
            directory = comp_dir if entry.dir_index == 0 else dirs[entry.dir_index - 1]
            index = i + 1
        path = os.path.join(directory, name)
        if not os.path.isabs(path) and comp_dir:
            path = os.path.join(comp_dir, path)
        table[index] = os.path.normpath(path)
    return table


def _static_address(die, address_size: int, little: bool) -> int | None:
    """Address of a variable whose location is a fixed address (DW_OP_addr)."""
    loc = die.attributes.get("DW_AT_location")
    if loc is None or loc.form != "DW_FORM_exprloc" or not loc.value or loc.value[0] != 0x03:
        return None
    raw = bytes(loc.value[1 : 1 + address_size])
    return int.from_bytes(raw, "little" if little else "big")
