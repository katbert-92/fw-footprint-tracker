"""Parsers for the two artefacts a GNU toolchain link produces.

MapParser reads the MEMORY block of a linker map file, which is where the
physical size of every region comes from -- nothing needs to be configured for
that. ElfParser reads section headers and symbols out of the ELF itself.
"""

import io
import re
from pathlib import Path

from elftools.elf.elffile import ELFFile

from .log import get_logger

logger = get_logger(__name__)


class MapParser:
    def __init__(self, map_file_path: Path) -> None:
        self.map_file_path = map_file_path
        self.map_lines = []
        self._load_map_data()

    def _load_map_data(self) -> None:
        logger.info(f"Loading map file: {self.map_file_path}")
        try:
            with open(self.map_file_path) as f:
                self.map_lines = f.readlines()
            logger.info(f"File {self.map_file_path} successfully loaded")
        except OSError as e:
            # Raised, not exited: this runs inside someone else's build system,
            # and SystemExit walks straight through an `except Exception` that
            # was meant to keep a footprint failure from stopping a build.
            raise OSError(f"Cannot read the map file {self.map_file_path}: {e}") from e

    def get_regions_info(self) -> dict:
        """Physical memory regions: name, origin and length as the linker sees them."""
        if not self.map_lines:
            logger.warning("Map data is empty")
            return {}

        section_re = re.compile(r"^(\S+)\s+(0x[0-9a-fA-F]+)\s+(0x[0-9a-fA-F]+)\s+\S+")
        memory_regions = {}

        header_index = None
        for i, line in enumerate(self.map_lines):
            if line.strip().startswith("Name") and "Origin" in line and "Length" in line:
                header_index = i
                break

        if header_index is None:
            logger.warning("Memory region section not found in map file")
            return {}

        for line in self.map_lines[header_index + 1 :]:
            if line.strip() == "" or line.strip().startswith("*default*"):
                continue

            match = section_re.match(line)
            if match:
                name, origin_str, length_str = match.groups()
                origin = int(origin_str, 16)
                length = int(length_str, 16)
                memory_regions[name] = {"origin": origin, "length": length}
                logger.debug(f"Parsed region: {name}, Origin: {origin_str}, Length: {length_str}")
            else:
                break

        logger.info(f"Parsed {len(memory_regions)} memory regions")
        return memory_regions


class ElfParser:
    def __init__(self, elf_path: Path) -> None:
        self.elf_path = elf_path
        self.elf_data = None
        self.elf = None
        self._load_elf_data()

    def _load_elf_data(self) -> None:
        logger.info(f"Loading ELF file: {self.elf_path}")
        try:
            with open(self.elf_path, "rb") as f:
                self.elf_data = f.read()
            self.elf = ELFFile(io.BytesIO(self.elf_data))
            logger.info(f"File {self.elf_path} successfully loaded")
        except OSError as e:
            raise OSError(f"Cannot read the ELF file {self.elf_path}: {e}") from e

    def get_sections_info(self) -> dict:
        if not self.elf:
            logger.error("ELF data not loaded properly")
            return {}

        section_info = {}
        for section in self.elf.iter_sections():
            section_info[section.name] = dict(section.header)

        logger.info(f"Extracted info from {len(section_info)} sections")
        return section_info

    def get_segments_info(self) -> list:
        """Loadable segments, the authoritative view of what the image occupies.

        p_paddr/p_filesz say where the bytes live in the image (flash), while
        p_vaddr/p_memsz say what is occupied while running (RAM). For a plain
        flash-resident segment the two coincide; for .data they differ, which is
        exactly how an initialiser gets charged to flash without anyone having
        to name the flash region.
        """
        if not self.elf:
            logger.error("ELF data not loaded properly")
            return []

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

        logger.info(f"Found {len(segments)} loadable segments")
        return segments

    def get_toolchain(self) -> str:
        """Compiler identification from .comment, when the toolchain emits it."""
        if not self.elf:
            return ""

        section = self.elf.get_section_by_name(".comment")
        if section is None:
            return ""

        entries = [e for e in section.data().decode("utf-8", "replace").split("\0") if e]
        return entries[0] if entries else ""

    def get_symbol_sizes(self) -> dict:
        if not self.elf:
            logger.error("ELF data not loaded properly")
            return {}

        function_sizes = {}
        variable_sizes = {}
        other_sizes = {}

        for section in self.elf.iter_sections():
            if section.header["sh_type"] == "SHT_SYMTAB":
                for symbol in section.iter_symbols():
                    if symbol.entry.st_size > 0:
                        symbol_type = symbol.entry["st_info"]["type"]

                        if symbol_type == "STT_FUNC":
                            function_sizes[symbol.name] = symbol.entry.st_size
                        elif symbol_type == "STT_OBJECT":
                            variable_sizes[symbol.name] = symbol.entry.st_size
                        else:
                            other_sizes[symbol.name] = symbol.entry.st_size

        logger.info(f"Collected {len(function_sizes)} functions, {len(variable_sizes)} variables")
        return {"functions": function_sizes, "variables": variable_sizes, "others": other_sizes}

    def get_function_addresses(self) -> dict:
        if not self.elf:
            logger.error("ELF data not loaded properly")
            return {}

        function_addresses = {}
        for section in self.elf.iter_sections():
            if section.header["sh_type"] == "SHT_SYMTAB":
                for symbol in section.iter_symbols():
                    if symbol.entry.st_info["type"] == "STT_FUNC":
                        function_addresses[symbol.name] = symbol.entry.st_value

        logger.info(f"Found {len(function_addresses)} function addresses")
        return function_addresses
