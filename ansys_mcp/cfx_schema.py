"""Versioned CFX recipe: one supervised product process per step operation.

Each recipe carries exactly one operation (``pre``, ``solve`` or ``post``).
A CFX pre/solve/post process is a heavyweight product invocation; keeping it in
its own step keeps process status, timeout, log tail and produced artifacts
attributable to one supervised command.
"""
from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CFXStrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CFXOutputSpec(CFXStrictModel):
    name: str = Field(min_length=1, max_length=128)
    path: str


class CFXPreOperation(CFXStrictModel):
    op: Literal["pre"]
    # Trusted CFX-Pre batch session (.pre) or CCL fragment (.ccl) executed by
    # cfx5pre -batch. The script itself decides which .def is written.
    script: str
    def_path: str | None = None


class CFXSolveOperation(CFXStrictModel):
    op: Literal["solve"]
    def_path: str
    # Declared expectations. They are verified after the run; their absence is
    # reported, never inferred from a zero exit code.
    res_path: str | None = None
    out_path: str | None = None
    run_name: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
    precision: Literal["single", "double"] = "double"
    partitions: int = Field(default=1, ge=1, le=1024)
    start_method: str | None = Field(default=None, min_length=1, max_length=128)
    continue_from: str | None = None
    initial_file: str | None = None
    ccl_path: str | None = None

    @model_validator(mode="after")
    def one_starting_point(self) -> "CFXSolveOperation":
        if self.continue_from and self.initial_file:
            raise ValueError("solve accepts continue_from or initial_file, not both.")
        return self


class CFXPostOperation(CFXStrictModel):
    op: Literal["post"]
    # Trusted CFD-Post session (.cse). The script writes the declared outputs.
    script: str
    result_file: str | None = None
    outputs: list[CFXOutputSpec] = Field(default_factory=list)


CFXOperation = Annotated[
    Union[CFXPreOperation, CFXSolveOperation, CFXPostOperation],
    Field(discriminator="op"),
]


class CFXRecipe(CFXStrictModel):
    version: Literal["1"] = "1"
    operation: CFXOperation
    # RMS targets belong to CFX-Pre physics settings, not to solver CLI flags.
