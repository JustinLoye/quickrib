import logging
from collections import Counter, deque
from datetime import datetime
from enum import IntFlag, auto
from typing import Optional

from quickrib.elements import ParsedElement, RIBNodeData, WithdrawalElement
from quickrib.observers.observer import Observer
from quickrib.rib_table import RIBTable

logger = logging.getLogger(__name__)


class RIBTablePathHistory(RIBTable):
    """A RIB flavor that remembers each prefix's recent AS paths.

    Route-flap detection needs to know where a prefix was *before* the update
    before last, which a single ``old_data`` cannot say. This keeps a bounded
    ``as-path-history`` deque on the node and is otherwise an ordinary
    :class:`~quickrib.rib_table.RIBTable`: it overrides only the enrichment
    hook, so the write and the notification contract stay in one place.
    """

    def __init__(self, history_size: int = 2):
        # 2 is the minimum history size that can show a path returning to where
        # it was, which is what route flapping looks like.
        self.history_size = history_size
        super().__init__()

    def _enrich_announcement(self, data, old_data) -> None:
        if not old_data:
            return
        history = data.get("as-path-history")
        if history is None:
            data["as-path-history"] = deque(
                [old_data["as-path"]], maxlen=self.history_size
            )
        else:
            history.append(old_data["as-path"])


class UpdateTagFlags(IntFlag):
    """
    Non-exclusive BGP update tags. 
    One update can have multiple flags simultaneously.
    """
    NONE = 0
    UPDATE_MESSAGE = auto()
    
    # Change size
    REMOVE_PREFIX = auto() #
    NEW_PREFIX = auto() #
    # Update entry -> AS Path
    ORIGIN_CHANGE = auto() #
    TRANSIT_CHANGE = auto()
    PATH_SWITCHING = auto() #
    PREPENDING_ADD = auto() # 
    PREPENDING_CHANGE = auto() #
    PREPENDING_REMOVE = auto() #
    # Update entry -> Other attributes
    COMMUNITY_CHANGE = auto() #
    OTHER_ATTRIBUTE_CHANGE = auto()
    # No change
    DUPLICATE_WITHDRAWAL = auto()
    DUPLICATE_ANNOUNCE = auto()

class UpdateTagger(Observer):
    """Tags every update with the BLT classification flags it satisfies.

    The tags are *returned*, not printed: a subclass such as
    :class:`UpdateTagsCounter` accumulates them, and the calling program decides
    what to do with the result. Set ``log_tags`` to trace individual messages
    through ``logging.DEBUG``. That is one record per BGP message, so it is off
    by default and belongs on a narrow filter, not a full replay.
    """

    def __init__(self, name: str = "update_tagger", log_tags: bool = False):
        self.name = name
        self.log_tags = log_tags

    def _log(self, bgpelem, flags: "UpdateTagFlags") -> None:
        if self.log_tags:
            logger.debug("%s %s", bgpelem, self._get_tag_names(flags))

    
    @staticmethod
    def _get_tag_names(flags: UpdateTagFlags) -> str:
        active_tags = [
            tag.name
            for tag in UpdateTagFlags
            if tag & flags and tag.name is not None and tag.name != "NONE"
        ]
        return " | ".join(active_tags) if active_tags else "NONE"

    def update_rib(self, bgpelem: ParsedElement):
        pass
        
    def update_withdrawal(self, bgpelem: WithdrawalElement, data: Optional[RIBNodeData]) -> UpdateTagFlags:
        if data:
            flags = UpdateTagFlags.REMOVE_PREFIX | UpdateTagFlags.UPDATE_MESSAGE
        else:
            flags = UpdateTagFlags.DUPLICATE_WITHDRAWAL | UpdateTagFlags.UPDATE_MESSAGE
        self._log(bgpelem, flags)
        return flags

    
    def update_announcement(self, bgpelem: ParsedElement, data: RIBNodeData, old_data: Optional[RIBNodeData]) -> UpdateTagFlags:
        """
        Calculates non-exclusive tags for an announcement.
        """
        # Handle New Prefix (Exclusive to other logic for efficiency)
        if not old_data:
            flags = UpdateTagFlags.NEW_PREFIX | UpdateTagFlags.UPDATE_MESSAGE
            self._log(bgpelem, flags)
            return flags
        
        # Accumulate non-exclusive tags
        flags = UpdateTagFlags.NONE
        flags |= UpdateTagFlags.UPDATE_MESSAGE
        new_path = data["as-path"]
        old_path = old_data["as-path"]
        
        # Path switching logic (Using the injected rnode context)
        if history := data.get("as-path-history"):
            try:
                # If current path matches the path from two updates ago
                if history[-2] == new_path and old_path != new_path:
                    flags |= UpdateTagFlags.PATH_SWITCHING
            except IndexError:
                pass
        
        # Origin or Transit change logic
        if old_path != new_path:
            if old_path[-1] != new_path[-1]:
                flags |= UpdateTagFlags.ORIGIN_CHANGE
            else:
                flags |= UpdateTagFlags.TRANSIT_CHANGE

        # Community Change logic
        if old_data["communities"] != data["communities"]:
            flags |= UpdateTagFlags.COMMUNITY_CHANGE
            
        # Prepending logic
        new_path_diff = len(new_path) - len(set(new_path))
        old_path_diff = len(old_path) - len(set(old_path))
        if new_path_diff or old_path_diff:
            if new_path_diff > old_path_diff:
                flags |= UpdateTagFlags.PREPENDING_ADD
                flags |= UpdateTagFlags.PREPENDING_CHANGE
            elif new_path_diff < old_path_diff:
                flags |= UpdateTagFlags.PREPENDING_REMOVE
                flags |= UpdateTagFlags.PREPENDING_CHANGE
            elif new_path_diff == old_path_diff and new_path != old_path:
                flags |= UpdateTagFlags.PREPENDING_CHANGE

        # If after all checks flags is still NONE, it was likely a duplicate/identical announce
        if flags == UpdateTagFlags.UPDATE_MESSAGE and old_path == new_path:
            flags = UpdateTagFlags.UPDATE_MESSAGE | UpdateTagFlags.DUPLICATE_ANNOUNCE
        self._log(bgpelem, flags)
        return flags
    
    def dump(self, ts: datetime) -> Optional[dict[str, int]]:
        return None


class UpdateTagsCounter(UpdateTagger):
    """Counts how often each tag fires, and reports the totals once per window."""

    def __init__(self, name: str = "update_tags_counter", log_tags: bool = False):
        super().__init__(name, log_tags=log_tags)
        self.tag_counts: Counter[str] = Counter()
        # Pre-cache flag members to avoid .__dict__ or .members calls in hot loop.
        # `Flag.name` is Optional because a *composite* flag has none; every
        # member here is a single named one, hence the `or ""` formality.
        self._tag_list: list[tuple[UpdateTagFlags, str]] = [
            (f, f.name or "") for f in UpdateTagFlags if f is not UpdateTagFlags.NONE
        ]

    def _apply_counts(self, flags: UpdateTagFlags) -> None:
        # Iterating the value yields only the members that are set: one step per
        # tag that fired, rather than one test per tag that exists.
        for flag in flags:
            name = flag.name
            if name is not None:
                self.tag_counts[name] += 1
                
    def update_rib(self, bgpelem: ParsedElement):
        pass

    def update_withdrawal(self, bgpelem: WithdrawalElement, data: Optional[RIBNodeData]) -> UpdateTagFlags:
        tags = super().update_withdrawal(bgpelem, data)
        self._apply_counts(tags)
        return tags
    
    def update_announcement(self, bgpelem: ParsedElement, data: RIBNodeData, old_data: Optional[RIBNodeData]) -> UpdateTagFlags:
        tags = super().update_announcement(bgpelem, data, old_data)
        self._apply_counts(tags)
        return tags
    
    def dump(self, ts: datetime) -> dict[str, int]:
        """Return this window's tag totals, and start the next window empty.

        Every tag is present, including the ones that did not fire: a caller
        reading a series wants a zero, not a missing key.
        """
        counts = {name: self.tag_counts.get(name, 0) for _flag, name in self._tag_list}
        logger.info(
            "Update tags for the window ending %s: %s",
            ts,
            {name: count for name, count in counts.items() if count} or "none",
        )
        self.tag_counts.clear()
        return counts