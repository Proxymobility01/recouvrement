import unicodedata


def remove_accents(texte):
    if not texte:
        return ""
    return ''.join(
        c for c in unicodedata.normalize('NFD', str(texte))
        if unicodedata.category(c) != 'Mn'
    ).lower()