# Copyright 2024 DataRobot, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

import json
from typing import Any, Callable, Generator, Literal

import pandas as pd
import plotly.graph_objects as go
from openai.types.chat.chat_completion_assistant_message_param import (
    ChatCompletionAssistantMessageParam,
)
from openai.types.chat.chat_completion_message_param import ChatCompletionMessageParam
from openai.types.chat.chat_completion_system_message_param import (
    ChatCompletionSystemMessageParam,
)
from openai.types.chat.chat_completion_user_message_param import (
    ChatCompletionUserMessageParam,
)
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    GetJsonSchemaHandler,
    ValidationInfo,
    computed_field,
    field_validator,
    model_validator,
)

from utils.code_execution import MaxReflectionAttempts


class LLMDeploymentSettings(BaseModel):
    target_feature_name: str = "resultText"
    prompt_feature_name: str = "promptText"


class AiCatalogDataset(BaseModel):
    id: str
    name: str
    created: str
    size: str


class DataFrameWrapper:
    def __init__(self, df: pd.DataFrame) -> None:
        self.df = df

    def to_dict(self) -> list[dict[str, Any]]:
        records = self.df.to_dict(orient="records")
        records_str = [{str(k): v for k, v in record.items()} for record in records]
        return records_str

    @classmethod
    def __get_validators__(
        cls,
    ) -> Generator[Callable[[Any, ValidationInfo], DataFrameWrapper], None, None]:
        yield cls.validate

    @classmethod
    def validate(cls, v: Any, info: ValidationInfo) -> "DataFrameWrapper":
        # Accept an already wrapped instance.
        if isinstance(v, cls):
            return v
        if isinstance(v, pd.DataFrame):
            return cls(v)
        elif isinstance(v, list):
            try:
                df = pd.DataFrame.from_records(v)
                return cls(df)
            except Exception as e:
                raise ValueError(
                    "Invalid data format; expecting a list of records"
                ) from e
        raise ValueError("data must be either a pandas DataFrame or a list of records")

    @classmethod
    def __get_pydantic_json_schema__(
        cls, core_schema: dict[str, Any], handler: GetJsonSchemaHandler
    ) -> dict[str, Any]:
        # This schema is used only if the field were included.
        # We mark the field as excluded in the model, so it will not appear.
        return {
            "title": "DataFrameWrapper",
            "type": "array",
            "items": {"type": "object"},
            "description": "Internal representation of data as a list of records (excluded from output)",
        }


class AnalystDataset(BaseModel):
    name: str = "analyst_dataset"
    # The internal data field stores the DataFrame wrapped in DataFrameWrapper.
    # It is excluded from the output and from the OpenAPI schema.
    data: DataFrameWrapper = Field(
        default_factory=lambda: DataFrameWrapper(pd.DataFrame()),
        exclude=True,
        description="Internal field storing the pandas DataFrame",
    )

    model_config = ConfigDict(arbitrary_types_allowed=True)

    @computed_field(
        title="Data Records",
        description="This field returns the data from the internal pandas DataFrame as a list of record dictionaries.",
        examples=[[{"a": 1, "b": "x"}, {"a": 2, "b": "y"}]],
        json_schema_extra={"type": "array", "items": {"type": "object"}},
        return_type=list[dict[str, Any]],
    )
    def data_records(self) -> list[dict[str, Any]]:
        return self.data.to_dict()

    @model_validator(mode="before")
    @classmethod
    def reconstruct_data(cls, values: dict[str, Any]) -> dict[str, Any]:
        """
        If the input JSON does not include 'data' but includes 'data_records',
        reconstruct the internal DataFrame from the records.
        """
        if "data" not in values and "data_records" in values:
            try:
                records = values["data_records"]
                df = pd.DataFrame.from_records(records)
                # Wrap the DataFrame before storing it.
                values["data"] = DataFrameWrapper(df)
            except Exception as e:
                raise ValueError(
                    "Invalid data_records for DataFrame reconstruction"
                ) from e
        return values

    def to_df(self) -> pd.DataFrame:
        """Return the internal pandas DataFrame."""
        return self.data.df

    @property
    def columns(self) -> list[str]:
        return self.data.df.columns.tolist()


class CleansedColumnReport(BaseModel):
    new_column_name: str
    original_column_name: str | None = None
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    original_dtype: str | None = None
    new_dtype: str | None = None
    conversion_type: str | None = None


class CleansedDataset(BaseModel):
    dataset: AnalystDataset
    cleaning_report: list[CleansedColumnReport]

    @property
    def name(self) -> str:
        return self.dataset.name

    def to_df(self) -> pd.DataFrame:
        return self.dataset.to_df()


class DataDictionaryColumn(BaseModel):
    data_type: str
    column: str
    description: str


class DataDictionary(BaseModel):
    name: str
    column_descriptions: list[DataDictionaryColumn]

    @classmethod
    def from_analyst_df(
        cls,
        df: pd.DataFrame,
        name: str = "analysis_result",
        column_descriptions: str = "Analysis result column",
    ) -> "DataDictionary":
        return DataDictionary(
            name=name,
            column_descriptions=[
                DataDictionaryColumn(
                    column=col,
                    description=column_descriptions,
                    data_type=str(df[col].dtype),
                )
                for col in df.columns
            ],
        )

    @classmethod
    def from_application_df(
        cls, df: pd.DataFrame, name: str = "analysis_result"
    ) -> "DataDictionary":
        columns = {"column", "description", "data_type"}
        if not columns.issubset(df.columns):
            raise ValueError(f"DataFrame must contain columns: {columns}")

        column_descriptions = [
            DataDictionaryColumn(
                column=row["column"],
                description=row["description"],
                data_type=row["data_type"],
            )
            for _, row in df.iterrows()
        ]

        return DataDictionary(name=name, column_descriptions=column_descriptions)

    def to_application_df(self) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "column": [c.column for c in self.column_descriptions],
                "description": [c.description for c in self.column_descriptions],
                "data_type": [c.data_type for c in self.column_descriptions],
            }
        )


class DictionaryGeneration(BaseModel):
    """Validates LLM responses for data dictionary generation

    Attributes:
        columns: List of column names
        descriptions: List of column descriptions

    Raises:
        ValueError: If validation fails
    """

    columns: list[str]
    descriptions: list[str]

    @field_validator("descriptions")
    @classmethod
    def validate_descriptions(cls, v: Any, values: Any) -> Any:
        # Check if columns exists in values
        if "columns" not in values.data:
            raise ValueError("Columns must be provided before descriptions")

        # Check if lengths match
        if len(v) != len(values.data["columns"]):
            raise ValueError(
                f"Number of descriptions ({len(v)}) must match number of columns ({len(values['columns'])})"
            )

        # Validate each description
        for desc in v:
            if not desc or not isinstance(desc, str):
                raise ValueError("Each description must be a non-empty string")
            if len(desc.strip()) < 10:
                raise ValueError("Descriptions must be at least 10 characters long")

        return v

    @field_validator("columns")
    @classmethod
    def validate_columns(cls, v: Any) -> Any:
        if not v:
            raise ValueError("Columns list cannot be empty")

        # Check for duplicates
        if len(v) != len(set(v)):
            raise ValueError("Duplicate column names are not allowed")

        # Validate each column name
        for col in v:
            if not col or not isinstance(col, str):
                raise ValueError("Each column name must be a non-empty string")

        return v

    def to_dict(self) -> dict[str, str]:
        """Convert columns and descriptions to dictionary format

        Returns:
            Dict mapping column names to their descriptions
        """
        return dict(zip(self.columns, self.descriptions))


class RunAnalysisRequest(BaseModel):
    datasets: list[AnalystDataset]
    dictionaries: list[DataDictionary]
    question: str


class RunAnalysisResultMetadata(BaseModel):
    duration: float
    attempts: int
    datasets_analyzed: int | None = None
    total_rows_analyzed: int | None = None
    total_columns_analyzed: int | None = None
    exception: AnalysisError | None = None


class RunAnalysisResult(BaseModel):
    status: Literal["success", "error"]
    metadata: RunAnalysisResultMetadata
    dataset: AnalystDataset | None = None
    code: str | None = None


class CodeExecutionError(BaseModel):
    code: str | None = None
    exception_str: str | None = None
    stdout: str | None = None
    stderr: str | None = None
    traceback_str: str | None = None


class AnalysisError(BaseModel):
    exception_history: list[CodeExecutionError] | None = None

    @classmethod
    def from_max_reflection_exception(
        cls,
        exception: MaxReflectionAttempts,
    ) -> "AnalysisError":
        return AnalysisError(
            exception_history=[
                CodeExecutionError(
                    exception_str=str(exception.exception),
                    traceback_str=exception.traceback_str,
                    code=exception.code,
                    stdout=exception.stdout,
                    stderr=exception.stderr,
                )
                for exception in exception.exception_history
                if exception is not None
            ]
            if exception.exception_history is not None
            else None,
        )


class RunDatabaseAnalysisResultMetadata(BaseModel):
    duration: float
    attempts: int
    datasets_analyzed: int | None = None
    total_columns_analyzed: int | None = None
    exception: AnalysisError | None = None


class RunDatabaseAnalysisResult(BaseModel):
    status: Literal["success", "error"]
    metadata: RunDatabaseAnalysisResultMetadata
    dataset: AnalystDataset | None = None
    code: str | None = None


class ChartGenerationExecutionResult(BaseModel):
    fig1: go.Figure
    fig2: go.Figure

    model_config = ConfigDict(arbitrary_types_allowed=True)


class RunChartsRequest(BaseModel):
    dataset: AnalystDataset
    question: str


class RunChartsResult(BaseModel):
    status: Literal["success", "error"]
    fig1_json: str | None = None
    fig2_json: str | None = None
    code: str | None = None
    metadata: RunAnalysisResultMetadata

    @property
    def fig1(self) -> go.Figure | None:
        return go.Figure(json.loads(self.fig1_json)) if self.fig1_json else None

    @property
    def fig2(self) -> go.Figure | None:
        return go.Figure(json.loads(self.fig2_json)) if self.fig2_json else None


class GetBusinessAnalysisMetadata(BaseModel):
    duration: float | None = None
    question: str | None = None
    rows_analyzed: int | None = None
    columns_analyzed: int | None = None
    exception_str: str | None = None


class BusinessAnalysisGeneration(BaseModel):
    bottom_line: str
    additional_insights: str
    follow_up_questions: list[str]


class GetBusinessAnalysisResult(BaseModel):
    status: Literal["success", "error"]
    bottom_line: str
    additional_insights: str
    follow_up_questions: list[str]
    metadata: GetBusinessAnalysisMetadata | None = None


class GetBusinessAnalysisRequest(BaseModel):
    dataset: AnalystDataset
    dictionary: DataDictionary
    question: str


class ChatRequest(BaseModel):
    """Request model for chat history processing

    Attributes:
        messages: list of dictionaries containing chat messages
                 Each message must have 'role' and 'content' fields
                 Role must be one of: 'user', 'assistant', 'system'
    """

    messages: list[ChatCompletionMessageParam] = Field(min_length=1)


class QuestionListGeneration(BaseModel):
    questions: list[str]


class ValidatedQuestion(BaseModel):
    """Stores validation results for suggested questions"""

    question: str


class RunDatabaseAnalysisRequest(BaseModel):
    datasets: list[AnalystDataset]
    dictionaries: list[DataDictionary]
    question: str = Field(min_length=1)


class DatabaseAnalysisCodeGeneration(BaseModel):
    code: str
    description: str


class EnhancedQuestionGeneration(BaseModel):
    enhanced_user_message: str


class CodeGeneration(BaseModel):
    code: str
    description: str


RuntimeCredentialType = Literal["llm", "db"]


DatabaseConnectionType = Literal["snowflake", "bigquery", "no_database"]


class AppInfra(BaseModel):
    llm: str
    database: DatabaseConnectionType


UserRoleType = Literal["assistant", "user", "system"]


class Tool(BaseModel):
    name: str
    signature: str
    docstring: str
    function: Callable[..., Any]

    def __str__(self) -> str:
        return f"function: {self.name}{self.signature}\n{self.docstring}\n\n"


class AnalystChatMessage(BaseModel):
    role: UserRoleType
    content: str
    components: list[
        RunAnalysisResult
        | RunChartsResult
        | GetBusinessAnalysisResult
        | EnhancedQuestionGeneration
        | RunDatabaseAnalysisResult
    ]

    def to_openai_message_param(self) -> ChatCompletionMessageParam:
        if self.role == "user":
            return ChatCompletionUserMessageParam(role=self.role, content=self.content)
        elif self.role == "assistant":
            return ChatCompletionAssistantMessageParam(
                role=self.role, content=self.content
            )
        elif self.role == "system":
            return ChatCompletionSystemMessageParam(
                role=self.role, content=self.content
            )