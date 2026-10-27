from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

from .mechanical_schema import MechanicalRecipe
from .dpf_schema import DPFRecipe
from .cfx_schema import CFXRecipe


ID_PATTERN = r"^[A-Za-z][A-Za-z0-9_-]{0,63}$"
METHOD_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]*$"
PathVariants = str | list[str]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RetrySpec(StrictModel):
    max_attempts: int = Field(default=1, ge=1, le=5)
    backoff_seconds: float = Field(default=1.0, ge=0, le=300)
    retry_on_codes: list[str] = Field(default_factory=list)


class ArtifactSpec(StrictModel):
    name: str | None = None
    path: str
    required: bool = True


class BaseWorkflowStep(StrictModel):
    id: str | None = Field(default=None, pattern=ID_PATTERN)
    continue_on_error: bool | None = None
    timeout_seconds: float | None = Field(default=None, gt=0)
    retry: RetrySpec = Field(default_factory=RetrySpec)
    produces: list[ArtifactSpec] = Field(default_factory=list)


class CommandStep(BaseWorkflowStep):
    type: Literal["command"]
    command_id: str
    args: list[Any] = Field(default_factory=list)
    detach: bool = False

    @model_validator(mode="after")
    def detached_has_no_outputs(self) -> "CommandStep":
        if self.detach and self.produces:
            raise ValueError("Detached commands cannot promise completed artifacts; use a supervised command.")
        return self


class PythonScriptStep(BaseWorkflowStep):
    type: Literal["python_script"]
    script_path: str
    arguments: list[Any] = Field(default_factory=list)


class PyFluentReadSpec(StrictModel):
    method: Literal["auto", "case", "case_data"] = "auto"
    case: str | None = None
    case_file: str | None = None
    case_data: str | None = None

    @model_validator(mode="after")
    def require_input(self) -> "PyFluentReadSpec":
        if not (self.case or self.case_file or self.case_data):
            raise ValueError("PyFluent read requires case, case_file, or case_data.")
        return self


class PyFluentWriteSpec(StrictModel):
    case: str | None = None
    case_data: str | None = None

    @model_validator(mode="after")
    def require_output(self) -> "PyFluentWriteSpec":
        if not (self.case or self.case_data):
            raise ValueError("PyFluent write requires case or case_data.")
        return self


class BasePyFluentOperation(StrictModel):
    store_as: str | None = Field(default=None, pattern=ID_PATTERN)
    continue_on_error: bool = False


class ReadOperation(BasePyFluentOperation):
    op: Literal["read"]
    method: Literal["auto", "case", "case_data"] = "auto"
    case: str | None = None
    case_file: str | None = None
    case_data: str | None = None

    @model_validator(mode="after")
    def require_input(self) -> "ReadOperation":
        if not (self.case or self.case_file or self.case_data):
            raise ValueError("PyFluent read operation requires case, case_file, or case_data.")
        return self


class WriteOperation(BasePyFluentOperation):
    op: Literal["write"]
    case: str | None = None
    case_data: str | None = None

    @model_validator(mode="after")
    def require_output(self) -> "WriteOperation":
        if not (self.case or self.case_data):
            raise ValueError("PyFluent write operation requires case or case_data.")
        return self


class SetStateOperation(BasePyFluentOperation):
    op: Literal["set_state"]
    path: PathVariants
    value: Any = None


class SetConstantOperation(BasePyFluentOperation):
    op: Literal["set_constant"]
    path: PathVariants
    value: Any = None


class GetStateOperation(BasePyFluentOperation):
    op: Literal["get_state"]
    path: PathVariants


class CallOperation(BasePyFluentOperation):
    op: Literal["call"]
    path: PathVariants
    args: list[Any] = Field(default_factory=list)
    kwargs: dict[str, Any] = Field(default_factory=dict)


class TuiOperation(CallOperation):
    op: Literal["tui"]


class IterateOperation(BasePyFluentOperation):
    op: Literal["iterate"]
    iterations: int = Field(gt=0)


class CalculateOperation(BasePyFluentOperation):
    op: Literal["calculate"]


class CreateOperation(BasePyFluentOperation):
    op: Literal["create"]
    path: PathVariants
    name: str
    state: dict[str, Any] | None = None


class ComputeReportsOperation(BasePyFluentOperation):
    op: Literal["compute_reports"]
    names: list[str] | None = None
    report_defs: list[str] | None = None

    @model_validator(mode="after")
    def require_names(self) -> "ComputeReportsOperation":
        if not (self.names or self.report_defs):
            raise ValueError("compute_reports requires names or report_defs.")
        return self


class InspectPathOperation(BasePyFluentOperation):
    op: Literal["inspect_path"]
    path: PathVariants


class MeshCheckAlias(BasePyFluentOperation):
    op: Literal["mesh_check"]


class SetZoneTypeAlias(BasePyFluentOperation):
    op: Literal["set_zone_type"]
    zone_list: list[str]
    new_type: str


class CreateForceReportAlias(BasePyFluentOperation):
    op: Literal["create_force_report"]
    name: str
    force_vector: list[float] | None = None
    zones: list[str] | None = None
    per_selection: bool | None = None
    report_output_type: str | None = None


PyFluentOperation = Annotated[
    Union[
        ReadOperation,
        WriteOperation,
        SetStateOperation,
        SetConstantOperation,
        GetStateOperation,
        CallOperation,
        TuiOperation,
        IterateOperation,
        CalculateOperation,
        CreateOperation,
        ComputeReportsOperation,
        InspectPathOperation,
        MeshCheckAlias,
        SetZoneTypeAlias,
        CreateForceReportAlias,
    ],
    Field(discriminator="op"),
]


class PyFluentStep(BaseWorkflowStep):
    type: Literal["pyfluent"]
    launch: dict[str, Any] = Field(default_factory=dict)
    read: str | PyFluentReadSpec | None = None
    operations: list[PyFluentOperation] = Field(default_factory=list)
    write: PyFluentWriteSpec | None = None


class WorkbenchProjectSpec(StrictModel):
    open_path: str | None = None
    save_path: str | None = None
    overwrite: bool = False


class BaseWorkbenchAction(StrictModel):
    id: str | None = Field(default=None, pattern=ID_PATTERN)
    continue_on_error: bool | None = None


class GetTemplateAction(BaseWorkbenchAction):
    op: Literal["get_template"]
    template_name: str
    solver: str | None = None
    store_as: str = Field(pattern=METHOD_PATTERN)


class CreateSystemAction(BaseWorkbenchAction):
    op: Literal["create_system"]
    template: str = Field(pattern=METHOD_PATTERN)
    args: list[Any] = Field(default_factory=list)
    kwargs: dict[str, Any] = Field(default_factory=dict)
    store_as: str = Field(pattern=METHOD_PATTERN)


class GetSystemAction(BaseWorkbenchAction):
    op: Literal["get_system"]
    name: str
    store_as: str = Field(pattern=METHOD_PATTERN)


class GetComponentAction(BaseWorkbenchAction):
    op: Literal["get_component"]
    target: str = Field(pattern=METHOD_PATTERN)
    name: str
    store_as: str = Field(pattern=METHOD_PATTERN)


class GetContainerAction(BaseWorkbenchAction):
    op: Literal["get_container"]
    target: str = Field(pattern=METHOD_PATTERN)
    component_name: str
    store_as: str = Field(pattern=METHOD_PATTERN)


class CallAction(BaseWorkbenchAction):
    op: Literal["call"]
    target: str = Field(pattern=METHOD_PATTERN)
    method: str = Field(pattern=METHOD_PATTERN)
    args: list[Any] = Field(default_factory=list)
    kwargs: dict[str, Any] = Field(default_factory=dict)
    store_as: str | None = Field(default=None, pattern=METHOD_PATTERN)


class GetAttrAction(BaseWorkbenchAction):
    op: Literal["get_attr"]
    target: str = Field(pattern=METHOD_PATTERN)
    attribute: str = Field(pattern=METHOD_PATTERN)
    store_as: str = Field(pattern=METHOD_PATTERN)


class SetAttrAction(BaseWorkbenchAction):
    op: Literal["set_attr"]
    target: str = Field(pattern=METHOD_PATTERN)
    attribute: str = Field(pattern=METHOD_PATTERN)
    value: Any = None


class SendCommandAction(BaseWorkbenchAction):
    op: Literal["send_command"]
    target: str = Field(pattern=METHOD_PATTERN)
    script_path: str | None = None
    code: str | None = None
    language: str | None = None
    store_as: str | None = Field(default=None, pattern=METHOD_PATTERN)

    @model_validator(mode="after")
    def exactly_one_source(self) -> "SendCommandAction":
        if (self.script_path is None) == (self.code is None):
            raise ValueError("send_command requires exactly one of script_path or code.")
        return self


class RunScriptAction(BaseWorkbenchAction):
    op: Literal["run_script"]
    target: str = Field(pattern=METHOD_PATTERN)
    script_path: str | None = None
    code: str | None = None
    suffix: Literal[".py", ".scscript", ".js"] = ".py"
    store_as: str | None = Field(default=None, pattern=METHOD_PATTERN)

    @model_validator(mode="after")
    def exactly_one_source(self) -> "RunScriptAction":
        if (self.script_path is None) == (self.code is None):
            raise ValueError("run_script requires exactly one of script_path or code.")
        return self


WorkbenchAction = Annotated[
    Union[
        GetTemplateAction,
        CreateSystemAction,
        GetSystemAction,
        GetComponentAction,
        GetContainerAction,
        CallAction,
        GetAttrAction,
        SetAttrAction,
        SendCommandAction,
        RunScriptAction,
    ],
    Field(discriminator="op"),
]


class WorkbenchRecipe(StrictModel):
    version: Literal["1"] = "1"
    script_version: str | None = None
    project: WorkbenchProjectSpec = Field(default_factory=WorkbenchProjectSpec)
    actions: list[WorkbenchAction] = Field(min_length=1)
    continue_on_error: bool = False

    @model_validator(mode="after")
    def validate_references(self) -> "WorkbenchRecipe":
        handles: set[str] = set()
        action_ids: set[str] = set()
        for index, action in enumerate(self.actions):
            action_id = action.id or f"action_{index + 1}"
            if action_id in action_ids:
                raise ValueError(f"Duplicate Workbench action id: {action_id}")
            action_ids.add(action_id)
            for reference in _action_references(action):
                if reference not in handles:
                    raise ValueError(f"Workbench action {action_id} references unknown handle: {reference}")
            store_as = getattr(action, "store_as", None)
            if store_as:
                if store_as in handles:
                    raise ValueError(f"Duplicate Workbench handle: {store_as}")
                handles.add(store_as)
        return self


class WorkbenchStep(BaseWorkflowStep):
    type: Literal["workbench"]
    mode: Literal["batch", "interactive"] = "batch"
    recipe: WorkbenchRecipe | None = None
    recipe_path: str | None = None

    @model_validator(mode="after")
    def exactly_one_recipe(self) -> "WorkbenchStep":
        if (self.recipe is None) == (self.recipe_path is None):
            raise ValueError("workbench step requires exactly one of recipe or recipe_path.")
        return self


class FileStep(BaseWorkflowStep):
    type: Literal["file"]
    action: Literal["exists", "read_text", "write_text", "copy"] = "exists"
    path: str | None = None
    source: str | None = None
    target: str | None = None
    content: str | None = None

    @model_validator(mode="after")
    def validate_fields(self) -> "FileStep":
        if self.action in {"exists", "read_text"} and not self.path:
            raise ValueError(f"file action {self.action} requires path.")
        if self.action == "write_text" and not (self.target or self.path):
            raise ValueError("file action write_text requires target or path.")
        if self.action == "copy" and not (self.source and self.target):
            raise ValueError("file action copy requires source and target.")
        return self


class MechanicalStep(BaseWorkflowStep):
    type: Literal["mechanical"]
    recipe: MechanicalRecipe


class DPFStep(BaseWorkflowStep):
    type: Literal["dpf"]
    recipe: DPFRecipe


class CFXStep(BaseWorkflowStep):
    type: Literal["cfx"]
    recipe: CFXRecipe


class AssertStep(BaseWorkflowStep):
    type: Literal["assert"]
    path: str
    exists: bool = True


class CollectStep(BaseWorkflowStep):
    type: Literal["collect"]
    artifacts: list[ArtifactSpec] = Field(min_length=1)


WorkflowStep = Annotated[
    Union[CommandStep, PythonScriptStep, PyFluentStep, WorkbenchStep, MechanicalStep, DPFStep, CFXStep, FileStep, AssertStep, CollectStep],
    Field(discriminator="type"),
]
WORKFLOW_STEP_ADAPTER = TypeAdapter(WorkflowStep)


class WorkflowModel(StrictModel):
    version: str | int = "1"
    workdir: str | None = None
    variables: dict[str, Any] = Field(default_factory=dict)
    steps: list[WorkflowStep] = Field(min_length=1)
    artifacts: list[ArtifactSpec] = Field(default_factory=list)
    continue_on_error: bool = False

    @field_validator("version")
    @classmethod
    def validate_version(cls, value: str | int) -> str | int:
        if str(value) not in {"1", "1.0"}:
            raise ValueError("Only workflow version 1 is supported.")
        return value

    @model_validator(mode="after")
    def validate_step_ids(self) -> "WorkflowModel":
        seen: set[str] = set()
        for index, step in enumerate(self.steps):
            step_id = effective_step_id(step, index)
            if step_id in seen:
                raise ValueError(f"Duplicate workflow step id: {step_id}")
            seen.add(step_id)
        return self


def effective_step_id(step: BaseWorkflowStep, index: int) -> str:
    return step.id or f"step_{index + 1}"


def _action_references(action: BaseWorkbenchAction) -> set[str]:
    references: set[str] = set()
    for field in ("target", "template"):
        value = getattr(action, field, None)
        if value:
            references.add(value)
    if hasattr(action, "args"):
        references.update(_value_references(getattr(action, "args")))
    if hasattr(action, "kwargs"):
        references.update(_value_references(getattr(action, "kwargs")))
    if hasattr(action, "value"):
        references.update(_value_references(getattr(action, "value")))
    return references


def _value_references(value: Any) -> set[str]:
    if isinstance(value, dict):
        if set(value) == {"$ref"} and isinstance(value["$ref"], str):
            return {value["$ref"]}
        found: set[str] = set()
        for child in value.values():
            found.update(_value_references(child))
        return found
    if isinstance(value, list):
        found = set()
        for child in value:
            found.update(_value_references(child))
        return found
    return set()
