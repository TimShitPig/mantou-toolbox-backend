class ProviderError(RuntimeError):
    """平台可对外报告的错误代码，不包含请求凭据或原始响应。"""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def validate_catalog_count(book: dict, actual: int) -> None:
    import re

    value = str(book.get('chapterCount') or '').replace(',', '').strip()
    match = re.fullmatch(r'(\d+)\s*章?', value)
    declared = int(match.group(1)) if match else 0
    if declared > 0 and declared != actual:
        raise ProviderError('chapter_catalog_incomplete')
