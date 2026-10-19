"""Wire shapes for the staff search.

    POST /aggregation/v1/staff/registry/search   who is this beneficiary, where, which tabs
    POST /aggregation/v1/staff/registry/tab      one tab of one record, when it is opened

camelCase on the wire, like the Beneficiary-360 request the partner path takes.

Tabs, sections and fields are the registries' own metadata, passed through:
a client renders whatever comes and must ignore fields it does not know.
Changes within ``schemaVersion`` "1" only add; a breaking change gets a new path.
"""
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "1"


class _Camel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")


# ── search ──────────────────────────────────────────────────────────────────


class StaffSearchRequest(_Camel):
    foundational_id: str = Field(
        alias="foundationalId", min_length=1, max_length=255,
        description="The beneficiary's foundational ID (e.g. Fayda FAN, with or without "
                    "its FAN- prefix)")
    registry_filter: Optional[List[str]] = Field(
        default=None, alias="registryFilter",
        description="Registry codes to search; all staff-enabled registries when absent")
    purpose: Optional[str] = Field(
        default=None, max_length=255,
        description="Why the beneficiary is looked up; recorded in the audit log")


class StaffTabRequest(_Camel):
    search_id: str = Field(alias="searchId", min_length=1, max_length=8192)
    registry_code: str = Field(alias="registryCode", min_length=1, max_length=128)
    register_id: str = Field(alias="registerId", min_length=1, max_length=128)
    record_id: str = Field(alias="recordId", min_length=1, max_length=255)
    tab_id: str = Field(alias="tabId", min_length=1, max_length=255)
    page: int = Field(default=1, ge=1, description="Page of every list section (1-based)")
    page_size: Optional[int] = Field(
        default=None, alias="pageSize", ge=1,
        description="Rows per list section; default and cap are set by the service")


class StaffNextRequest(_Camel):
    """The call that opens a tab, ready to send as is (same bearer token)."""
    method: Literal["POST"] = "POST"
    url: str = Field(description="Path on this service")
    body: Dict[str, Any]


class StaffTabRef(_Camel):
    tab_id: str = Field(alias="tabId")
    label: str = Field(description="The registry's tab label (often an i18n key)")
    order: int
    request: StaffNextRequest


class StaffDisplayField(_Camel):
    name: str
    value: Any = None
    order: int = 0


class StaffRecordSummary(_Camel):
    """The registry's list row for the record: no extra call to build it."""
    functional_id: Optional[str] = Field(default=None, alias="functionalId")
    name: Optional[str] = None
    foundational_id: Optional[str] = Field(default=None, alias="foundationalId")
    link_foundational_id: Optional[str] = Field(default=None, alias="linkFoundationalId")
    image_url: Optional[str] = Field(default=None, alias="imageUrl")
    created_at: Optional[str] = Field(default=None, alias="createdAt")
    last_approved_at: Optional[str] = Field(default=None, alias="lastApprovedAt")
    display_fields: List[StaffDisplayField] = Field(default_factory=list, alias="displayFields")


class StaffRecord(_Camel):
    register: str
    register_id: str = Field(alias="registerId")
    record_id: str = Field(alias="recordId")
    # This record's ticket (also in each tab request): opens its tabs only.
    search_id: Optional[str] = Field(default=None, alias="searchId")
    summary: StaffRecordSummary
    tabs: List[StaffTabRef]


class StaffRegistryError(_Camel):
    reason: str
    detail: str = ""


class StaffRegistryResult(_Camel):
    registry_code: str = Field(alias="registryCode")
    registry_name: str = Field(alias="registryName")
    # incomplete: no record found in the pages read (staff_api_max_pages), but
    # the registry has more, so absence is not established.
    status: Literal["found", "not_found", "incomplete", "error"]
    records: List[StaffRecord]
    # False when the registry had more pages than were read: there may be
    # further records of this person.
    complete: bool = True
    error: Optional[StaffRegistryError] = None


class StaffSearchResponse(_Camel):
    schema_version: str = Field(default=SCHEMA_VERSION, alias="schemaVersion")
    search_id: str = Field(
        alias="searchId",
        description="Signed search ticket (staff user, expiry): names the search. Tabs are "
                    "opened with each record's own searchId, already in its tab requests")
    foundational_id: str = Field(alias="foundationalId")
    searched_at: str = Field(alias="searchedAt")
    searched_by: str = Field(alias="searchedBy")
    expires_at: str = Field(alias="expiresAt", description="Tabs can be opened until then")
    purpose: Optional[str] = None
    registries: List[StaffRegistryResult]


# ── tab ─────────────────────────────────────────────────────────────────────


class StaffField(_Camel):
    """One field of a section, from the registry's section UI schema."""
    key: str = Field(description="Key in each record")
    label: Optional[str] = None
    type: Optional[str] = Field(default=None, description="The registry's widget, e.g. text, "
                                                          "date, number, select")
    order: int
    options: Optional[List[Dict[str, Any]]] = Field(
        default=None, description="Fixed choices {label, value}, when the registry lists them")


class StaffPage(_Camel):
    page: int
    page_size: int = Field(alias="pageSize")
    total_items: int = Field(alias="totalItems")
    total_pages: int = Field(alias="totalPages")


class StaffSection(_Camel):
    section_id: str = Field(alias="sectionId")
    mnemonic: Optional[str] = None
    label: Optional[str] = None
    order: int
    is_list: bool = Field(alias="isList")
    register_id: str = Field(alias="registerId", description="The section's own register")
    register: Optional[str] = None
    fields: List[StaffField]
    records: List[Dict[str, Any]]
    pagination: Optional[StaffPage] = Field(default=None, description="List sections only")


class StaffTabResponse(_Camel):
    schema_version: str = Field(default=SCHEMA_VERSION, alias="schemaVersion")
    search_id: str = Field(alias="searchId")
    registry_code: str = Field(alias="registryCode")
    register_id: str = Field(alias="registerId")
    record_id: str = Field(alias="recordId")
    tab_id: str = Field(alias="tabId")
    label: str
    sections: List[StaffSection]
