# -*- coding: utf-8 -*-
import json
import os

from RaidBook_common import write_json_atomic


class UserStore:
    """user_mapping.json / uid_alias.json helper bound to a Data directory."""

    def __init__(self, data_dir, logger=None):
        self.data_dir = data_dir
        self.logger = logger
        self.user_mapping_path = os.path.join(data_dir, "user_mapping.json")
        self.uid_alias_path = os.path.join(data_dir, "uid_alias.json")
        self._logged_alias_hits = set()

    def load_mapping(self):
        if not os.path.exists(self.user_mapping_path):
            return {}
        try:
            with open(self.user_mapping_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def save_mapping(self, mapping):
        write_json_atomic(self.user_mapping_path, mapping, self.logger)

    def update_mapping(self, uid, name):
        mapping = self.load_mapping()
        str_id = str(uid)
        if mapping.get(str_id) != name:
            mapping[str_id] = name
            self.save_mapping(mapping)
            return True
        return False

    def id_to_name(self):
        return self.load_mapping()

    def name_to_uid(self):
        return {v: k for k, v in self.load_mapping().items()}

    def load_alias(self):
        if not os.path.exists(self.uid_alias_path):
            return {}
        try:
            with open(self.uid_alias_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            return {str(k): str(v) for k, v in raw.items() if not str(k).startswith("_")}
        except Exception:
            return {}

    def alias_notes(self):
        if not os.path.exists(self.uid_alias_path):
            return {}
        try:
            with open(self.uid_alias_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            return raw.get("_notes", {}) or {}
        except Exception:
            return {}

    def resolve_uid(self, uid):
        if uid is None:
            return ""
        alias = self.load_alias()
        notes = self.alias_notes()
        original = str(uid)
        current = original
        for _ in range(5):
            if current in alias and alias[current] != current:
                current = alias[current]
            else:
                break
        if current != original:
            key = (original, current)
            if key not in self._logged_alias_hits:
                self._logged_alias_hits.add(key)
                if self.logger:
                    self.logger.info(f"[alias] {original} → {current} | {notes.get(original, 'alias')}")
        return current

    def auto_migrate_mapping(self):
        if not os.path.exists(self.user_mapping_path):
            return 0
        try:
            mapping = self.load_mapping()
            if not mapping:
                return 0
            sample_key = next(iter(mapping))
            sample_val = mapping[sample_key]
            if not str(sample_key).isdigit() and str(sample_val).isdigit():
                new_mapping = {str(v): k for k, v in mapping.items()}
                self.save_mapping(new_mapping)
                return len(new_mapping)
        except Exception as e:
            if self.logger:
                self.logger.info(f"[유저 매핑] 자동 마이그레이션 실패: {e}")
        return 0
