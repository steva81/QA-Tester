from enum import Enum
import urllib.parse
from app.qa.semantic_model import SemanticControl

class RiskClassification(Enum):
    OBSERVE_ONLY = "OBSERVE_ONLY"
    SAFE_CANDIDATE = "SAFE_CANDIDATE"
    POTENTIALLY_MUTATING = "POTENTIALLY_MUTATING"
    PROHIBITED = "PROHIBITED"
    UNKNOWN = "UNKNOWN"

class InteractionSafetyPolicy:
    DESTRUCTIVE_HREF_SIGNATURES = ['delete', 'logout', 'remove', 'revoke', 'purge', 'destroy', 'wipe', 'terminate']
    MUTATING_HREF_SIGNATURES = ['update', 'save', 'submit']

    @classmethod
    def classify(cls, control: SemanticControl) -> RiskClassification:
        if not control.visibility or not control.enabled:
            return RiskClassification.OBSERVE_ONLY

        if not control.accessible_role or not control.accessible_name:
            return RiskClassification.UNKNOWN

        name_lower = control.accessible_name.lower()
        destructive_names = ['delete account', 'remove account', 'revoke', 'purge', 'destroy', 'wipe', 'terminate']
        if any(sig in name_lower for sig in destructive_names):
            return RiskClassification.PROHIBITED

        if control.accessible_role in ['button', 'input'] and any(sig in name_lower for sig in ['submit', 'save', 'update', 'upload']):
            return RiskClassification.POTENTIALLY_MUTATING

        if control.href:
            href_unquoted = urllib.parse.unquote(control.href).lower()
            href_raw_lower = control.href.lower()

            for sig in cls.DESTRUCTIVE_HREF_SIGNATURES:
                if sig in href_unquoted:
                    return RiskClassification.PROHIBITED

            for sig in cls.MUTATING_HREF_SIGNATURES:
                if sig in href_unquoted:
                    return RiskClassification.POTENTIALLY_MUTATING

            if href_raw_lower.startswith('javascript:'):
                if 'location' in href_unquoted or 'delete' in href_unquoted:
                    return RiskClassification.PROHIBITED
                return RiskClassification.UNKNOWN

            if href_raw_lower.startswith('data:') or href_raw_lower.startswith('mailto:') or href_raw_lower.startswith('tel:'):
                return RiskClassification.UNKNOWN

            if control.target == '_blank':
                return RiskClassification.UNKNOWN

            # Unknown scheme
            if '://' in href_raw_lower and not (href_raw_lower.startswith('http://') or href_raw_lower.startswith('https://')):
                return RiskClassification.UNKNOWN

            if any(sig in name_lower for sig in ['delete', 'remove', 'submit', 'save', 'upload']):
                return RiskClassification.POTENTIALLY_MUTATING

            if control.accessible_role == 'link':
                return RiskClassification.SAFE_CANDIDATE

            return RiskClassification.UNKNOWN

        if control.accessible_role == 'tab':
            if any(sig in name_lower for sig in ['delete account', 'remove account']):
                return RiskClassification.PROHIBITED
            return RiskClassification.SAFE_CANDIDATE

        if control.accessible_role == 'menuitem':
            # Blocker 6: Require positive menu-action eligibility.
            # A menuitem role and absence of a few dangerous words are insufficient.
            # Require a supported action pattern or explicit contract consistent with safety policy.
            # Unknown actions remain unsupported.
            if any(sig in name_lower for sig in ['delete', 'remove', 'submit', 'save', 'upload', 'update', 'purge', 'revoke']):
                return RiskClassification.POTENTIALLY_MUTATING
            if control.form_ownership:
                return RiskClassification.POTENTIALLY_MUTATING

            # 1. State toggle pattern (expanded, selected, or checked)
            if control.state_expanded is not None or control.state_selected is not None or control.state_checked is not None:
                return RiskClassification.SAFE_CANDIDATE

            # 2. Volatile metadata containing supported pattern indicator
            if any("controls:" in m or "aria-controls:" in m or "supported_pattern" in m for m in (control.volatile_metadata or ())):
                return RiskClassification.SAFE_CANDIDATE

            # 3. Explicit registered contract
            from app.qa.validation_contract import find_validation_contract_for_control
            if find_validation_contract_for_control(control.control_id, control.accessible_name) is not None:
                return RiskClassification.SAFE_CANDIDATE

            return RiskClassification.UNKNOWN

        if control.accessible_role in ['button', 'combobox', 'link']:
            if any(sig in name_lower for sig in ['delete', 'remove', 'submit', 'save', 'upload']):
                return RiskClassification.POTENTIALLY_MUTATING
            return RiskClassification.SAFE_CANDIDATE

        return RiskClassification.UNKNOWN
