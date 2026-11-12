from pydantic import BaseModel


class TaskStatusResponse(BaseModel):
    task_id: str
    paper_id: str | None
    status: str
    stage: str | None = None
    progress: float
    error_message: str | None = None

