"""Read-only Greenhouse application form inspector."""

# The JavaScript DOM probe is intentionally kept as one executable snippet.
# ruff: noqa: E501

from __future__ import annotations

import logging
from typing import Any

from playwright.async_api import Page

from app.applicants.base import ApplicantAdapter
from app.schemas.application import (
    ApplicationField,
    ApplicationFieldType,
    ApplicationForm,
    ApplicationOption,
)

logger = logging.getLogger(__name__)


class GreenhouseApplicant(ApplicantAdapter):
    async def inspect(self, page: Page, *, job_id: int) -> ApplicationForm:
        """Extract controls and labels using one read-only DOM evaluation."""
        raw: list[dict[str, Any]] = await page.evaluate(r"""
            () => {
              const controls = [...document.querySelectorAll('input, textarea, select')].filter(el => {
                if (el.getAttribute('aria-hidden') === 'true') return false;
                if (el.tagName !== 'INPUT') return true;
                return !['hidden','submit','button','reset','image'].includes((el.type || '').toLowerCase());
              });
              const text = n => (n?.textContent || '').replace(/\s+/g, ' ').trim();
              const labelFor = el => {
                const id = el.id;
                const explicit = id && document.querySelector(`label[for="${CSS.escape(id)}"]`);
                if (explicit) return text(explicit).replace(/\s*[*]\s*$/, '');
                const enclosing = el.closest('label');
                if (enclosing) return text(enclosing).replace(/\s*[*]\s*$/, '');
                const aria = el.getAttribute('aria-label');
                if (aria) return aria.trim();
                const labelled = el.getAttribute('aria-labelledby');
                if (labelled) { const value = labelled.split(/\s+/).map(x => document.getElementById(x)).map(text).filter(Boolean).join(' '); if (value) return value; }
                const parent = el.closest('.field, .form-field, .application-question, fieldset, .question');
                const nearby = parent?.querySelector('legend, .label, .field-label, .question-label, h3, h4');
                if (nearby && nearby !== el) return text(nearby).replace(/\s*[*]\s*$/, '');
                return el.getAttribute('placeholder')?.trim() || el.name || el.id || 'Unnamed field';
              };
              const groupLabel = el => { const parent = el.closest('fieldset, .field, .form-field, .application-question, .question'); const legend = parent?.querySelector('legend, .label, .field-label, .question-label, h3, h4'); return legend ? text(legend).replace(/\s*[*]\s*$/, '') : labelFor(el); };
              const typeOf = el => { if (el.tagName === 'TEXTAREA') return 'textarea'; if (el.tagName === 'SELECT' || (el.getAttribute('role') === 'combobox' && ['true', 'listbox'].includes(el.getAttribute('aria-haspopup')))) return 'select'; const nativeType = ({email:'email',tel:'tel',file:'file',date:'date',number:'number',radio:'radio',checkbox:'checkbox',text:'text'}[el.type] || 'unknown'); const identity = `${el.id || ''} ${el.name || ''} ${el.autocomplete || ''}`.toLowerCase(); if (nativeType === 'text' && /(^|[\s_-])email([\s_-]|$)/.test(identity)) return 'email'; return nativeType; };
              return controls.map((el, index) => { const parent = el.closest('.field, .form-field, .application-question, fieldset, .question'); const marked = parent && /required|field-required/.test(parent.className); const explicit = el.id && document.querySelector(`label[for="${CSS.escape(el.id)}"]`); const rawLabel = explicit ? text(explicit) : text(el.closest('label')); const markerText = rawLabel || (parent ? text(parent.querySelector('legend, .label, .field-label, .question-label, h3, h4')) : ''); const star = /[*]\s*$/.test(markerText); return { id: el.id || el.name || `field_${index + 1}`, name: el.name || '', label: labelFor(el), group_label: groupLabel(el), type: typeOf(el), required: !!(el.required || el.getAttribute('aria-required') === 'true' || marked || star), value: el.value || null, options: [...(el.options || [])].map(o => ({value: o.value || null, label: text(o)})), group: el.type === 'radio' || el.type === 'checkbox' ? (el.name || el.id || `field_${index + 1}`) : null }; });
            }
            """)
        fields: list[ApplicationField] = []
        grouped: dict[tuple[str, str], ApplicationField] = {}
        for item in raw:
            kind = ApplicationFieldType(item["type"])
            group_key = (str(item["group"]), item["type"]) if item["group"] else None
            options = [ApplicationOption.model_validate(option) for option in item["options"]]
            if group_key:
                existing = grouped.get(group_key)
                if existing:
                    existing.options.extend(
                        options
                        or [ApplicationOption(value=item.get("value"), label=str(item["label"]))]
                    )
                    existing.required = existing.required or bool(item["required"])
                    continue
                field = ApplicationField(
                    id=str(item["group"]),
                    label=str(item.get("group_label") or item["label"]),
                    field_type=kind,
                    required=bool(item["required"]),
                    options=options
                    or [ApplicationOption(value=item.get("value"), label=str(item["label"]))],
                    selector_hint=f'[name="{item["group"]}"]',
                )
                grouped[group_key] = field
            else:
                field = ApplicationField(
                    id=str(item["id"]),
                    label=str(item["label"]),
                    field_type=kind,
                    required=bool(item["required"]),
                    options=options,
                    selector_hint=f'#{item["id"]}' if item["id"] else None,
                )
            fields.append(field)
        logger.info("Discovered %d fields for application job_id=%s", len(fields), job_id)
        return ApplicationForm(ats="greenhouse", job_id=job_id, url=page.url, fields=fields)


__all__ = ["GreenhouseApplicant"]
