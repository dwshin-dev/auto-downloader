#!/bin/bash
# '영상 다운로더.app'을 다시 만든다. 앱소스.applescript나 아이콘을 고쳤을 때만 돌리면 된다.
#   bash tools/앱만들기.sh
set -e
cd "$(dirname "$0")/.."

APP="영상 다운로더.app"

rm -rf "$APP"
# -s = stay-open. 실행이 끝나도 앱이 독에 남아서 우클릭 → 종료로 끌 수 있다.
osacompile -s -o "$APP" tools/앱소스.applescript

# 아이콘 교체
cp tools/앱아이콘.icns "$APP/Contents/Resources/applet.icns"

# 앱 이름 (독에 마우스 올리면 나오는 이름)
/usr/libexec/PlistBuddy -c "Set :CFBundleName 영상 다운로더" "$APP/Contents/Info.plist" 2>/dev/null || \
  /usr/libexec/PlistBuddy -c "Add :CFBundleName string 영상 다운로더" "$APP/Contents/Info.plist"

# 번들 ID가 없으면 맥이 앱을 이름으로 못 찾아서 '종료'가 먹통이 된다
/usr/libexec/PlistBuddy -c "Add :CFBundleIdentifier string com.dwshin.video-downloader" \
  "$APP/Contents/Info.plist" 2>/dev/null || true

# plist를 고쳤으니 서명을 다시 한다 (안 하면 앱이 안 열린다)
codesign --force --sign - "$APP" 2>/dev/null || true

# 아이콘 캐시를 무시하고 새 아이콘이 바로 보이게
touch "$APP"

echo "✅ '$APP' 만들었어요"
