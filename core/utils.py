import unicodedata


def remove_accents(texte):
    if not texte:
        return ""
    return ''.join(
        c for c in unicodedata.normalize('NFD', str(texte))
        if unicodedata.category(c) != 'Mn'
    ).lower()


def format_phone_cm(phone):
    """
    Nettoie et force le formatage des numéros de téléphone au standard camerounais (237XXXXXXXXX).
    - Enlève les espaces, les signes '+' ou tirets.
    - Ajoute '237' si le numéro fait 9 chiffres.
    """
    if not phone:
        return ""

    # Enlever le +, les espaces et les tirets éventuels
    clean_phone = str(phone).replace("+", "").replace(" ", "").replace("-", "").strip()

    # Si le numéro est saisi sur 9 chiffres (ex: 690169694), on ajoute le code pays
    if len(clean_phone) == 9:
        return f"237{clean_phone}"

    return clean_phone