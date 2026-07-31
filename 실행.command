#!/bin/bash
# 더블클릭하면 다운로더가 실행되고 브라우저가 자동으로 열립니다
cd "$(dirname "$0")"

if [ ! -d venv ]; then
  echo "❗ 아직 설치가 안 됐어요. 먼저 '처음-설치.command'를 더블클릭해주세요."
  read -p "엔터를 누르면 창이 닫힙니다."
  exit 1
fi

source venv/bin/activate

# 2초 후 기본 브라우저로 화면 열기
(sleep 2 && open "http://127.0.0.1:5002") &

echo "🎬 영상 소스 다운로더를 시작합니다. 이 창은 닫지 마세요!"
echo "   (끝내려면 이 창을 닫으면 됩니다)"
python app.py
