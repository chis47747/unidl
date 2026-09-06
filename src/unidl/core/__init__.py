"""Core infrastructure shared by every service.

Nothing in here imports a UI framework: services and core stay renderer
agnostic, and the TUI is one possible front-end over the Flow protocol.
"""

from .cache import TokenStore
from .cdm import CdmError
from .chapters import Chapter
from .config import Config
from .credentials import Credential, CredentialSlot
from .flow import Back, Choice, FlowContext, Quit, run_flow
from .naming import format_title, save_name_for
from .partner import (
    PartnerAuthorization,
    PartnerAuthorizationError,
    PartnerAuthorizationResult,
    deliver_partner_authorization,
)
from .playback import DrmInfo, ExternalTrack, Playback, SubtitleReference
from .service import (
    CORE,
    SELF,
    AuthStatus,
    Capabilities,
    Service,
    ServiceContext,
    ServiceRegistry,
    registry,
)
from .settings import Option, Setting, Settings, SettingsStore
from .titles import Title, TitleKind

__all__ = [
    "CORE",
    "SELF",
    "AuthStatus",
    "Back",
    "Capabilities",
    "CdmError",
    "Chapter",
    "Choice",
    "Config",
    "Credential",
    "CredentialSlot",
    "DrmInfo",
    "ExternalTrack",
    "FlowContext",
    "Option",
    "PartnerAuthorization",
    "PartnerAuthorizationError",
    "PartnerAuthorizationResult",
    "Playback",
    "Quit",
    "Service",
    "ServiceContext",
    "ServiceRegistry",
    "Setting",
    "Settings",
    "SettingsStore",
    "SubtitleReference",
    "Title",
    "TitleKind",
    "TokenStore",
    "format_title",
    "deliver_partner_authorization",
    "registry",
    "run_flow",
    "save_name_for",
]
