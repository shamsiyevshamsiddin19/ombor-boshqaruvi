from django.conf import settings
from django.middleware.locale import LocaleMiddleware
from django.utils import translation

class CookieLocaleMiddleware(LocaleMiddleware):
    """Interface language comes only from the language cookie (set by the UZ/RU/EN switch).

    Unlike Django's LocaleMiddleware it ignores the browser's Accept-Language, so a
    worker's phone set to Russian does not switch the workshop app on its own:
    Uzbek stays the default until someone picks another language.
    """

    def process_request(self, request):
        lang = request.COOKIES.get(settings.LANGUAGE_COOKIE_NAME)
        if lang not in dict(settings.LANGUAGES):
            lang = settings.LANGUAGE_CODE
        translation.activate(lang)
        request.LANGUAGE_CODE = translation.get_language()


