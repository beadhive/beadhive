from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define

T = TypeVar("T", bound="AddCommentRequest")


@_attrs_define
class AddCommentRequest:
    """One comment to append. The issue is named by the path, so it is not a member here: a body carrying it too would give
    one request two spellings of one anchor and a question about what to do when they disagree.

        Attributes:
            author (str): Who is signing the comment. CALLER-ASSERTED, and not the authenticated principal — see the
                operation description.

                Trimmed of surrounding space, then refused when the result is empty, when it exceeds 256 bytes or 255 characters
                (the storage column), or when it carries a control character. The bounds and the character rule are `actor`'s,
                unchanged, because the value lands in a column of the same width that every renderer of the thread prints, where
                an unfiltered C1 introducer is an escape-sequence payload.
            text (str): The comment body, stored VERBATIM: newlines, surrounding space and unicode all survive, and nothing
                trims the value that lands in the row.

                NO LENGTH BOUND AND NO CHARACTER RULE, unlike `author` beside it, and both absences are the column: this one is
                `LONGTEXT` rather than a 255-character field, and a comment that is a stack trace or a diff is an ordinary
                comment. The only cap is the 1 MiB every body on this surface shares.

                BOTH PLANES AGREE ABOUT THAT, which is worth stating because they did not. `wisp_comments.text` was left `TEXT`
                — 65535 bytes — when the durable column was widened, so a comment past that limit wrote fine against an issue
                and failed against a wisp, on an operation that resolves its anchor across both planes deliberately. A caller
                therefore could not know which side of the bound it was on until the write failed. The ephemeral column is
                widened to match, so this member's bound is one number rather than two.

                Blank after trimming is a `400` — a comment of nothing but whitespace carries no information and is almost
                always a shell quoting accident — and blankness is judged on a TRIMMED COPY while the stored value is untrimmed,
                so a comment that merely begins with a newline is a comment.
    """

    author: str
    text: str

    def to_dict(self) -> dict[str, Any]:
        author = self.author

        text = self.text

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "author": author,
                "text": text,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        author = d.pop("author")

        text = d.pop("text")

        add_comment_request = cls(
            author=author,
            text=text,
        )

        return add_comment_request
