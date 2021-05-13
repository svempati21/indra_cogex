# -*- coding: utf-8 -*-

"""Processor for the INDRA database."""

import logging
import pickle
from pathlib import Path
from typing import Union

import humanize
import pandas as pd
import pystow

from indra.ontology.bio import bio_ontology
from indra_cogex.representation import Node, Relation
from indra_cogex.sources.processor import Processor

logger = logging.getLogger(__name__)


# If you don't have the data, get it from:
# 's3://bigmech/indra-db/dumps/2021-01-26/sif.pkl'


class DbProcessor(Processor):
    """Processor for the INDRA database."""

    name = "database"
    df: pd.DataFrame

    def __init__(self, path: Union[None, str, Path] = None):
        """Initialize the INDRA database processor.

        :param path: The path to the INDRA database SIF dump pickle. If none given, will look in the default location.
        """
        if path is None:
            path = pystow.join("indra", "db", name="sif.pkl")
        elif isinstance(path, str):
            path = Path(path)
        with open(path, "rb") as fh:
            df = pickle.load(fh)
        logger.info("Loaded %s rows from %s", humanize.intword(len(df)), path)
        self.df = df
        for side in "AB":
            # A lot of the names in the SIF dump are all over
            self.df[f"ag{side}_name"] = [
                bio_ontology.get_name(prefix, identifier)
                for prefix, identifier in self.df[
                    [f"ag{side}_ns", f"ag{side}_id"]
                ].values
            ]

    def get_nodes(self):  # noqa:D102
        df = pd.concat(
            [
                self.df[["agA_ns", "agA_id", "agA_name"]].rename(
                    {"agA_ns": "ns", "agA_id": "id", "agA_name": "name"}
                ),
                self.df[["agB_ns", "agB_id", "agB_name"]].rename(
                    {"agB_ns": "ns", "agB_id": "id", "agB_name": "name"}
                ),
            ],
            ignore_index=True,
        ).drop_duplicates()
        for db_ns, db_id, name in df.values:
            yield Node(db_ns, db_id, ["BioEntity"], dict(name=name))

    def get_relations(self):  # noqa:D102
        columns = ["A", "B", "stmt_type", "evidence_count", "stmt_hash"]
        for source, target, stmt_type, ev_count, stmt_hash in (
            self.df[columns].drop_duplicates().values
        ):
            data = {"stmt_hash:long": stmt_hash, "evidence_count:long": ev_count}
            yield Relation(source, target, [stmt_type], data)
