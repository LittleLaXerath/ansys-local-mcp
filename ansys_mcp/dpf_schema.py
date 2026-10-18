"""Strict, versioned recipes for local DPF post-processing.

There is deliberately no remote host, arbitrary Python, or security-disable
setting here.  A recipe owns its server and may only read/write allowed paths.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator


class DPFStrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DPFServerSpec(DPFStrictModel):
    transport: Literal["in_process", "grpc"] = "in_process"
    startup_timeout_seconds: float = Field(default=30, gt=0, le=600)


class DPFDataFile(DPFStrictModel):
    path: str
    key: str | None = None


class DPFOperationBase(DPFStrictModel):
    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")


class DPFDiscover(DPFOperationBase):
    op: Literal["discover"]
    name_filter: str | None = None
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=100, ge=1, le=1000)


class DPFMetadata(DPFOperationBase):
    op: Literal["metadata"]


class DPFResult(DPFOperationBase):
    op: Literal["result"]
    name: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]*$")
    time_scoping: int | float | list[int] | list[float] | None = None
    mesh_scoping: list[int] | None = None
    mesh_location: Literal["Nodal", "Elemental", "ElementalNodal"] = "Nodal"
    requested_location: Literal["Nodal", "Elemental", "ElementalNodal"] | None = None
    named_selection: str | None = None

    @model_validator(mode="after")
    def unambiguous_scoping(self) -> "DPFResult":
        if self.mesh_scoping is not None and self.named_selection is not None:
            raise ValueError("Specify mesh_scoping or named_selection, not both.")
        return self


class DPFOperator(DPFOperationBase):
    op: Literal["operator"]
    name: str = Field(min_length=1, max_length=256)
    inputs: dict[str, Any] = Field(default_factory=dict)
    output_pin: int | None = Field(default=None, ge=0)


class DPFInspect(DPFOperationBase):
    op: Literal["inspect"]
    source: str
    sample_entities: int = Field(default=0, ge=0, le=100)


class DPFExport(DPFOperationBase):
    op: Literal["export"]
    source: str
    path: str
    format: Literal["csv", "json"] = "csv"
    overwrite: bool = False


DPFOperation = Annotated[
    Union[DPFDiscover, DPFMetadata, DPFResult, DPFOperator, DPFInspect, DPFExport],
    Field(discriminator="op"),
]


class DPFRecipe(DPFStrictModel):
    version: Literal[1] = 1
    server: DPFServerSpec = Field(default_factory=DPFServerSpec)
    result_files: list[DPFDataFile] = Field(default_factory=list)
    operations: list[DPFOperation] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def check_data_flow(self) -> "DPFRecipe":
        ids: set[str] = {"data_sources"} if self.result_files else set()
        for operation in self.operations:
            if operation.id in ids:
                raise ValueError("DPF operation IDs must be unique; data_sources is reserved.")
            if operation.op in {"metadata", "result"} and not self.result_files:
                raise ValueError("DPF metadata/result operations require result_files.")
            if operation.op in {"inspect", "export"} and operation.source not in ids:
                raise ValueError("DPF source must refer to an earlier operation.")
            if operation.op == "operator":
                for reference in _references(operation.inputs):
                    if reference not in ids:
                        raise ValueError("DPF input references must refer to earlier operations.")
            ids.add(operation.id)
        return self


def _references(value: Any):
    if isinstance(value, dict):
        if "$ref" in value:
            if set(value) != {"$ref"} or not isinstance(value["$ref"], str):
                raise ValueError("A DPF reference must contain only a string $ref.")
            yield value["$ref"]
        else:
            for child in value.values():
                yield from _references(child)
    elif isinstance(value, list):
        for child in value:
            yield from _references(child)
