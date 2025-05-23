"""Generic class for tabular data."""

from __future__ import annotations

import contextlib
import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Optional, Sequence

import pandas as pd
from pydantic import BaseModel, ValidationError, model_validator
from typing_extensions import Self

from nipoppy.env import StrOrPathLike
from nipoppy.utils import save_df_with_backup


class BaseTabularModel(BaseModel):
    """
    Helper class for validating tabular data.

    Subclasses should define fields and their types,
    and optionally override the _validate_before_fields() method.

    Note: This class is called "model" to be consistent with Pydantic nomenclature,
    but it can be thought of as a schema for each row in the tabular file.
    """

    @model_validator(mode="before")
    @classmethod
    def validate_before(cls, data: Any):
        """Validate the raw input."""
        if isinstance(data, dict):
            # generic validation
            optional_fields = {
                field
                for field, info in cls.model_fields.items()
                if not info.is_required()
            }
            keys_to_remove = []
            for key, value in data.items():
                if (
                    isinstance(value, str) or not isinstance(value, Sequence)
                ) and pd.isna(value):
                    # remove optional fields with missing values
                    # so that the default is set correctly
                    if key in optional_fields:
                        keys_to_remove.append(key)

                    # otherwise use None for missing values
                    else:
                        data[key] = None

            for key in keys_to_remove:
                data.pop(key)

            # model-specific validation
            # to be overridden in subclass if needed
            data = cls._validate_before_fields(data)

        return data

    @classmethod
    def _validate_before_fields(cls, data: dict):
        """Validate model-specific fields. To be overridden in subclass if needed."""
        return data


class BaseTabular(pd.DataFrame, ABC):
    """
    Generic class with utilities for tabular data.

    See https://pandas.pydata.org/docs/development/extending.html.
    """

    _series_classes: dict = {}
    index_cols: list = []
    _metadata: list = []

    sep = "\t"

    @property
    @abstractmethod
    def model(self) -> type[BaseTabularModel]:
        """Model class associated with the tabular data."""
        raise NotImplementedError("model must be assigned in subclass")

    @classmethod
    def load(cls, fpath: StrOrPathLike, validate=True, **kwargs) -> Self:
        """Load (and optionally validate) a tabular data file."""
        if "dtype" in kwargs:
            raise ValueError(
                "This function does not accept 'dtype' as a keyword argument"
                ". Everything is read as a string and (optionally) validated later."
            )
        if "sep" in kwargs or "delimiter" in kwargs or "delim_whitespace" in kwargs:
            raise ValueError(
                "This function does not accept 'sep', 'delimiter', or "
                "'delim_whitespace' as keyword arguments. "
                f"The separator used is always '{cls.sep}'."
            )

        df = cls(pd.read_csv(fpath, dtype=str, sep=cls.sep, **kwargs))

        # heuristic to check if file is a CSV
        # because otherwise there would be obscure Pydantic validation errors
        if df.shape[1] == 1 and len(df.columns[0].split(",")) > 1:
            raise ValueError(
                f"It looks like the file at {fpath} might be a CSV instead of a TSV"
                " -- make sure the columns are separated by tabs, not commas"
            )

        if validate:
            df = df.validate()
        return df

    def __init__(self, *args, **kwargs) -> None:
        """Instantiate a tabular data object."""
        super().__init__(*args, **kwargs)

        # set column names if the dataframe is empty
        if self.empty and len(self.columns) == 0:
            for col in self.model.model_fields.keys():
                self[col] = None

    def validate(self) -> Self:
        """Validate the dataframe based on the model."""
        # for error messages
        name_processed = " ".join(
            re.findall("[A-Z][^A-Z]*", self.__class__.__name__)
        ).lower()

        records = self.to_dict(orient="records")
        try:
            df_validated = self.__class__(
                [self.model(**record).model_dump() for record in records],
            )

        except Exception as exception:
            error_message = str(exception)
            if isinstance(exception, ValidationError):
                error_message += str(exception.errors())
            raise ValueError(
                f"Error when validating the {name_processed} file: {error_message}"
            )

        if self.index_cols is not None:
            df_duplicated = df_validated.find_duplicates()
            if len(df_duplicated) > 0:
                raise ValueError(
                    f"Duplicate records found in the {name_processed} file"
                    f". Columns {self.index_cols} must uniquely identify a record"
                    f". Got duplicates:\n{df_duplicated}"
                )

        return df_validated

    def find_duplicates(self, cols=None) -> Self:
        """Find duplicate records."""
        if cols is None:
            cols = self.index_cols

        return self[self.duplicated(subset=cols, keep=False)]

    def get_diff(self, other: Self, cols=None) -> Self:
        """Get the difference between two dataframes (self - other).

        Returns a slice of self. If cols is None, the index_cols of the first
        object is used.
        """
        if cols is None:
            cols = self.index_cols

        for df in [self, other]:
            col_diff = set(cols) - set(df.columns)
            if len(col_diff) > 0:
                raise ValueError(
                    f"The columns {cols} are not present in the dataframe:\n{df}"
                )

        index_self = pd.Index(zip(*[self.loc[:, col] for col in cols]))
        index_other = pd.Index(zip(*[other.loc[:, col] for col in cols]))

        diff = self.loc[~index_self.isin(index_other)]

        return diff

    def add_or_update_records(self, records: list[dict] | dict, validate=True) -> Self:
        """Add or update records."""
        if isinstance(records, dict):
            records = [records]

        # identify non-index columns
        # need to do this before modifying the dataframe
        non_index_cols = set(self.columns) - set(self.index_cols)

        # set the index (temporary)
        self.set_index(self.index_cols, inplace=True)

        try:
            for record in records:
                # process record data
                if validate:
                    record = self.model(**record).model_dump()

                # add/update
                for col in non_index_cols:

                    # need to sort to avoid performance warning
                    self.sort_index(inplace=True)

                    idx = tuple(record[col] for col in self.index_cols)
                    if len(idx) == 1:
                        idx = idx[0]
                    self.loc[idx, col] = record[col]
        finally:
            self.reset_index(inplace=True)

        return self

    def concatenate(self, other: Self, validate=True) -> Self:
        """Concatenate two dataframes."""
        concatenated: Self = pd.concat([self, other], ignore_index=True)
        if validate:
            concatenated = concatenated.validate()
        return concatenated

    def save_with_backup(
        self,
        fpath_symlink: StrOrPathLike,
        dname_backups: Optional[str] = None,
        use_relative_path=True,
        sort=True,
        dry_run=False,
    ) -> Path | None:
        """Save the dataframe to a file with a backup."""
        tabular_new = self.sort_values() if sort else self
        if Path(fpath_symlink).exists():
            with contextlib.suppress(Exception):
                tabular_old = self.load(fpath_symlink)
                if sort:
                    tabular_old = tabular_old.sort_values()
                if tabular_new.equals(tabular_old):
                    return None
        return save_df_with_backup(
            tabular_new,
            fpath_symlink=fpath_symlink,
            dname_backups=dname_backups,
            use_relative_path=use_relative_path,
            dry_run=dry_run,
            sep=self.sep,
        )

    def equals(self, other: object) -> bool:
        """Check if two dataframes are equal."""
        try:
            pd.testing.assert_frame_equal(
                self,
                other,
                check_like=True,
                obj=str(self.__class__.__name__),
            )
            return True
        except AssertionError:
            return False

    def sort_values(self, **kwargs):
        """Sort the dataframe, by default on specific columns and ignoring the index."""
        sort_kwargs = {"by": self.index_cols, "ignore_index": True}
        sort_kwargs.update(kwargs)
        return super().sort_values(**sort_kwargs)

    def get_series_class(self) -> type[pd.Series]:
        """Get the series class associated with a dataframe."""
        tabular_class_id = id(self.__class__)
        if tabular_class_id not in self._series_classes:

            class _Series(pd.Series):
                @property
                def _constructor(_self):
                    return _Series

                @property
                def _constructor_expanddim(_self):
                    return self.__class__

            self._series_classes[tabular_class_id] = _Series

        return self._series_classes[tabular_class_id]

    @property
    def _constructor(self) -> type[pd.DataFrame]:
        """Override pd.DataFrame._constructor to return the subclass."""
        return self.__class__

    @property
    def _constructor_sliced(self) -> type[pd.Series]:
        """Override pd.DataFrame._constructor_sliced to return the series subclass."""
        return self.get_series_class()
