# -*- coding: utf-8 -*-

"""Processor for the InterPro database.

This was added in https://github.com/bgyori/indra_cogex/pull/125.

.. seealso:: https://ftp.ebi.ac.uk/pub/databases/interpro/current_release/
"""

import gzip
import logging
from collections import defaultdict
from pathlib import Path
from typing import Iterable, Mapping, Optional, Set, Tuple

import pandas as pd
import pystow
import requests
from protmapper import uniprot_client
from tqdm import tqdm

from ..processor import Processor
from ...representation import Node, Relation

__all__ = [
    "InterproProcessor",
]

logger = logging.getLogger(__name__)

INTERPRO_ENTRIES_URL = (
    "ftp://ftp.ebi.ac.uk/pub/databases/interpro/current_release/entry.list"
)
INTERPRO_SHORT_NAMES_URL = (
    "ftp://ftp.ebi.ac.uk/pub/databases/interpro/current_release/short_names.dat"
)
INTERPRO_TREE_URL = (
    "ftp://ftp.ebi.ac.uk/pub/databases/interpro/current_release/ParentChildTreeFile.txt"
)
INTERPRO_PROTEINS_URL = (
    "ftp://ftp.ebi.ac.uk/pub/databases/interpro/current_release/protein2ipr.dat.gz"
)
INTERPRO_GO_URL = (
    "ftp://ftp.ebi.ac.uk/pub/databases/interpro/current_release/interpro2go"
)


class InterproProcessor(Processor):
    """Processor for Interpro."""

    name = "interpro"
    node_types = ["BioEntity"]

    def __init__(self, force: bool = False):
        """Initialize the InterPro processor."""
        self.version = get_current_version()
        self.entries_df = get_entries_df(module=self.module)
        self.interpro_ids = set(self.entries_df["ENTRY_AC"])
        self.parents = get_parent_to_children(force=force, module=self.module)
        self.interpro_to_goa = get_interpro_to_goa(force=force, module=self.module)

        interpro_to_proteins = get_interpro_to_proteins(
            force=force, interpro_ids=self.interpro_ids, module=self.module
        )
        interpro_to_genes = defaultdict(set)
        for interpro_id, uniprot_ids in interpro_to_proteins.items():
            for uniprot_id, start, end in uniprot_ids:
                hgnc_id = uniprot_client.get_hgnc_id(uniprot_id)
                if hgnc_id is not None:
                    # there are a lot of TrEMBL entries, these will return none
                    interpro_to_genes[interpro_id].add((hgnc_id, start, end))
        self.interpro_to_genes = dict(interpro_to_genes)

    def get_nodes(self):  # noqa:D102
        unique_go = set()
        unique_hgnc = set()
        for interpro_id, _type, name, short_name in self.entries_df.values:
            yield Node(
                "IP",
                interpro_id,
                ["BioEntity"],
                dict(name=name, short_name=short_name, version=self.version),
            )
            unique_go.update(self.interpro_to_goa.get(interpro_id, set()))
            unique_hgnc.update(
                hgnc_id
                for hgnc_id, _, _ in self.interpro_to_genes.get(interpro_id, set())
            )
        for go_id in sorted(unique_go):
            yield Node("GO", go_id, ["BioEntity"])
        for hgnc_id in sorted(unique_hgnc, key=int):
            yield Node("HGNC", hgnc_id, ["BioEntity"])

    def get_relations(self):  # noqa:D102
        for interpro_id in sorted(self.interpro_ids):
            for child_interpro_id in sorted(self.parents.get(interpro_id, set())):
                yield Relation(
                    "IP", child_interpro_id, "IP", interpro_id, "isa"
                )

            for go_id in sorted(self.interpro_to_goa.get(interpro_id, [])):
                yield Relation("IP", interpro_id, "GO", go_id, "associated_with")

            for hgnc_id, start, end in sorted(
                self.interpro_to_genes.get(interpro_id, []), key=lambda t: int(t[0])
            ):
                yield Relation(
                    "HGNC",
                    hgnc_id,
                    "IP",
                    interpro_id,
                    "has_domain",
                    {"start:int": start, "end:int": end, "version": self.version},
                )


def get_entries_df(*, force: bool = False, module: pystow.Module) -> pd.DataFrame:
    """Get a dataframe of InterPro entries, filtered to domains."""
    short_names_df = module.ensure_csv(
        url=INTERPRO_SHORT_NAMES_URL,
        read_csv_kwargs=dict(
            header=None,
            names=("ENTRY_AC", "ENTRY_SHORT_NAME"),
        ),
        force=force,
    )

    df = module.ensure_csv(
        url=INTERPRO_ENTRIES_URL,
        read_csv_kwargs=dict(
            skiprows=1,
            names=("ENTRY_AC", "ENTRY_TYPE", "ENTRY_NAME"),
        ),
        force=force,
    )
    # Filter to entry types that represent domains
    df = df[df["ENTRY_TYPE"] == "Domain"]
    df = df.merge(short_names_df, on="ENTRY_AC", how="left")
    return df


def get_parent_to_children(
    *, force: bool = False, module: pystow.Module
) -> Mapping[str, Set[str]]:
    """The a mapping from parent InterPro ID to list of children InterPro IDs."""
    path = module.ensure(url=INTERPRO_TREE_URL, force=force)
    with open(path) as file:
        return _help_parent_to_children(file)


def _help_parent_to_children(lines: Iterable[str]) -> Mapping[str, Set[str]]:
    lines = [line.strip().split("::", 1)[0] for line in lines]
    parent_to_children = defaultdict(set)
    for parent_id, child_id in _unroll_tree(_format_tree(lines)):
        parent_to_children[parent_id].add(child_id)
    return dict(parent_to_children)


def _unroll_tree(d):
    for parent, subdict in d.items():
        for child, subsubdict in subdict.items():
            yield parent, child
            yield from _unroll_tree(subdict)


def _format_tree(lines):
    return {
        parent: _format_tree(child_lines)
        for parent, child_lines in descend(lines).items()
    }


def descend(lines):
    if not lines:
        return {}
    groups = {}
    # Assume the first one is always a root.
    parent, *lines = lines
    group = []
    for line in lines:
        if line.startswith("--"):
            group.append(line[2:])
        else:
            # save the previous group
            groups[parent] = group
            # set a new parent
            parent = line
            group = []
    # don't forget about the last one
    groups[parent] = group
    return groups


def _count_leading_dashes(s: str) -> int:
    """Count the number of leading dashes on a string."""
    for position, element in enumerate(s):
        if element != "-":
            return position
    raise ValueError


def get_interpro_to_proteins(
    *, force: bool = False, interpro_ids, module: pystow.Module
) -> Mapping[str, Set[Tuple[str, int, int]]]:
    """Get a mapping from InterPro identifiers to a set of UniProt identifiers."""
    cache_path = module.join(name="protein2ipr_human.tsv")

    if cache_path.is_file():
        rv = defaultdict(set)
        with cache_path.open() as file:
            for line in file:
                interpro_id, uniprot_id, start, end = line.strip().split("\t")
                rv[interpro_id].add((uniprot_id, int(start), int(end)))
        return dict(rv)

    path = module.ensure(url=INTERPRO_PROTEINS_URL, force=force)
    with gzip.open(path, "rt") as file:
        rv = _read_ipr2protein(file, interpro_ids)

    with cache_path.open("w") as file:
        for interpro_id, uniprot_ids in tqdm(
            sorted(rv.items()),
            unit_scale=True,
            desc="Writing human subset",
        ):
            for uniprot_id, start, end in sorted(uniprot_ids):
                print(interpro_id, uniprot_id, start, end, sep="\t", file=file)

    return rv


def _read_ipr2protein(file, interpro_ids):
    interpro_to_uniprots = defaultdict(set)
    for line in tqdm(
        file,
        unit_scale=True,
        unit="line",
        desc="Processing ipr2protein",
        total=1_216_508_710,
    ):
        uniprot_id, interpro_id, _name, _xref, start, end = line.split("\t")
        if interpro_id not in interpro_ids:
            continue
        if uniprot_client.is_human(uniprot_id):
            interpro_to_uniprots[interpro_id].add((uniprot_id, int(start), int(end)))
    return dict(interpro_to_uniprots)


def get_interpro_to_goa(
    *,
    force: bool = False,
    module: Optional[pystow.Module] = None,
    path: Optional[Path] = None,
) -> Mapping[str, Set[str]]:
    """Get a mapping from InterPro identifiers to sets of GO id/name pairs.."""
    if path is None:
        if module is None:
            raise ValueError
        path = module.ensure(url=INTERPRO_GO_URL, name="interpro2go.tsv", force=force)
    interpro_to_go_annotations = defaultdict(set)
    with path.open() as file:
        for line in file:
            line = line.strip()
            if line[0] == "!":
                continue
            interpro_id, go_id = process_go_mapping_line(line)
            interpro_to_go_annotations[interpro_id].add(go_id)
    return dict(interpro_to_go_annotations)


def process_go_mapping_line(line: str) -> Tuple[str, str]:
    """Process a GO mapping file line.

    Example lines:

    .. code-block::

        !date: 2022/10/05 11:07:08
        !Mapping of InterPro entries to GO
        !external resource: http://www.ebi.ac.uk/interpro
        !citation: Blum et al. (2021) Nucl. Acids Res. 49:D344â€“D354
        !contact:interhelp@ebi.ac.uk!
        InterPro:IPR000003 Retinoid X receptor/HNF4 > GO:DNA binding ; GO:0003677
        InterPro:IPR000003 Retinoid X receptor/HNF4 > GO:nuclear steroid receptor activity ; GO:0003707
    """
    line = line[len("InterPro:") :]
    line, go_id = (part.strip() for part in line.rsplit(";", 1))
    line, _go_name = (part.strip() for part in line.rsplit(">", 1))
    interpro_id, _interpro_name = (part.strip() for part in line.split(" ", 1))
    return interpro_id, go_id


def get_current_version() -> str:
    """Get the latest version of InterPro."""
    res = requests.get(
        "https://ftp.ebi.ac.uk/pub/databases/interpro/current_release/release_notes.txt"
    )
    release_lines = [
        line.strip() for line in res.text.splitlines() if line.startswith("Release ")
    ]
    # pick the second line
    line = release_lines[1]
    return line[len("Release ")].split(",", 1)[0]
