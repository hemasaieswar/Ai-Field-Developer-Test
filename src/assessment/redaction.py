import re

REPLACEMENT = "[REDACTED]"
EMAIL = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
SSN = re.compile(r"(?<![0-9])[0-9]{3}-[0-9]{2}-[0-9]{4}(?![0-9])")
CARD = re.compile(r"(?<![0-9])(?:[0-9][ -]?){12,18}[0-9](?![0-9])")


class Redactor:
    """Bounded two-stage scanner. No candidate is emitted before its boundary."""

    def __init__(self):
        self.word = ""
        self.word_overflow = False
        self.number = ""
        self.number_overflow = False

    def _numbers(self, text: str, final: bool = False) -> str:
        out = []
        for char in text:
            if char in "0123456789" or (self.number or self.number_overflow) and char in " -":
                if not self.number_overflow:
                    self.number += char
                    if len(self.number) > 128:
                        out.append(REPLACEMENT)
                        self.number = ""
                        self.number_overflow = True
            else:
                out.append(self._flush_number())
                out.append(char)
        if final:
            out.append(self._flush_number())
        return "".join(out)

    def _flush_number(self):
        result = (
            "" if self.number_overflow else CARD.sub(REPLACEMENT, SSN.sub(REPLACEMENT, self.number))
        )
        self.number = ""
        self.number_overflow = False
        return result

    def feed(self, text: str) -> str:
        out = []
        for char in text:
            if char.isascii() and (char.isalnum() or char in ".!#$%&'*+/=?^_`{|}~@-"):
                if not self.word_overflow:
                    self.word += char
                    if len(self.word) > 320:
                        out.append(self._numbers(REPLACEMENT))
                        self.word = ""
                        self.word_overflow = True
            else:
                out.append(self._flush_word())
                out.append(self._numbers(char))
        return "".join(out)

    def _flush_word(self):
        value = "" if self.word_overflow else EMAIL.sub(REPLACEMENT, self.word)
        self.word = ""
        self.word_overflow = False
        return self._numbers(value)

    def finish(self) -> str:
        return self._flush_word() + self._numbers("", final=True)
