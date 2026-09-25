from pydantic import Field,BaseModel
from typing import List,Annotated, Literal,Optional

class Task(BaseModel):
    id: int
    title: str
    goal: str = Field(..., description="One sentence describing what the reader should do/understand.")
    bullets: List[str] = Field(..., min_length=3, max_length=6)
    target_words: int = Field(..., description="Target words (200–400).")

    tags: List[str] = Field(default_factory=list)
    requires_research: bool = False
    requires_citations: bool = False
    requires_code: bool = False


class Plan(BaseModel):
    blog_title: str
    audience: str
    tone: str
    blog_kind: Literal["explainer", "tutorial", "news_roundup", "comparison", "system_design"] = "explainer"
    constraints: List[str] = Field(default_factory=list)
    tasks: List[Task]


class EvidenceItem(BaseModel): # every result from the reseacher results=info against the q is the max results is 2 for each q so like this for 5 q 10 results willbe there and it is the evidenceitem the ans contains or follows the schema of evidence item each ans will have the key written in the schema 
    title: str
    url: str # url if the info
    published_at: Optional[str] = None  # when it was publised 
    snippet: Optional[str] = None # the actual content means the info
    source: Optional[str] = None # source 


class RouterDecision(BaseModel): 
    needs_research: bool
    mode: Literal["closed_book", "hybrid", "open_book"]# open_book=needs research , closed=dont need research,hybrid =need research 
    reason: str
    queries: List[str] = Field(default_factory=list) # queries the router node  will produce 
    max_results_per_query: int = Field(5) # its  like a hyperparameter 


class EvidencePack(BaseModel): #it is the pack of the evidenceitem every results from the researcher is coverted to evidence item if the total resukt are 10 so 10 evidenceitem objects 
    evidence: List[EvidenceItem] = Field(default_factory=list)


# reducer/image schema 
class ImageSpec(BaseModel): # schema for the image if we need image the llm will give u the prompt , file_name where the image should be save etc this is for each image the imagespec will be diff for each placeholder that the llm will tell us 
    placeholder: str = Field(..., description="e.g. [[IMAGE_1]]")
    filename: str = Field(..., description="Save under images/, e.g. qkv_flow.png") # where the image willbe saved 
    alt: str
    caption: str
    prompt: str = Field(..., description="Prompt to send to the image model.") 
    size: Literal["1024x1024", "1024x1536", "1536x1024"] = "1024x1024" 
    quality: Literal["low", "medium", "high"] = "medium"
    related_section: str = Field(default="", description="The section heading this image illustrates, e.g. 'Loop Unrolling'")

# 
class GlobalImagePlan(BaseModel): #list of the image spec we will give to the generater node and md with placeholder and the prompt
    md_with_placeholders: str
    images: List[ImageSpec] = Field(default_factory=list) # have all the keys of imagespecs 
