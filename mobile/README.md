# Мобильные приложения (Android + iOS)

В проект добавлена заготовка мобильного приложения на базе **Capacitor**. Она оборачивает текущий сайт в нативный контейнер.

## Что это дает
- Android приложение (сборка APK/AAB через Android Studio)
- iOS приложение (сборка IPA через Xcode на macOS)

## 1) Подготовка

Укажите домен backend в `capacitor.config.ts`:

- `https://REPLACE_WITH_YOUR_DOMAIN` -> `https://ваш-домен`.

Backend должен быть доступен по HTTPS.

## 2) Установка зависимостей

```bash
cd mobile
npm install
```

## 3) Создание нативных проектов

```bash
npm run cap:add:android
npm run cap:add:ios
npm run cap:sync
```

## 4) Сборка Android установочного файла

```bash
npm run open:android
```

Далее в Android Studio:
- **Build > Build Bundle(s) / APK(s) > Build APK(s)** — получите `.apk`.
- **Build > Generate Signed Bundle / APK** — подпишите релиз и получите установочный файл для публикации.

## 5) Сборка iPhone установочного файла

```bash
npm run open:ios
```

Далее в Xcode (только macOS):
- Настройте Team/Signing.
- **Product > Archive**.
- Экспорт через Organizer -> получите `.ipa`.

## Важно

В этом репозитории добавлен именно **проект-обертка** для генерации установочных файлов. 
Готовые `.apk` и `.ipa` собираются локально в Android Studio / Xcode с вашей подписью.
