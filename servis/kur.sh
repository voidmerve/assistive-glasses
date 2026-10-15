#!/bin/sh
# Asistif Gözlük servisini kurar (yönetici şifresi gerekmez, kullanıcı servisi).
#
#   sh servis/kur.sh              # kurar; programı elle başlatırsın
#   sh servis/kur.sh --otomatik   # kurar ve Pi her açıldığında programı kendiliğinden başlatır
#
# Kurduktan sonra:
#   systemctl --user start asistif-gozluk      # başlat
#   systemctl --user stop asistif-gozluk       # durdur
#   systemctl --user restart asistif-gozluk    # yeniden başlat (kod değişince)
#   tail -f ~/Desktop/Gemını/gozluk.log         # canlı çıktı (Ctrl+C sadece izlemeyi bırakır)
#   systemctl --user disable asistif-gozluk    # açılışta otomatik başlamayı kapat
#
# Not: Servis çalışırken programı ayrıca "python3 son_mapli.py" ile başlatma: kamera meşgul olur.
set -e
DIZIN="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$HOME/.config/systemd/user"
cp "$DIZIN/asistif-gozluk.service" "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
echo "✓ Servis kuruldu: asistif-gozluk"
if [ "$1" = "--otomatik" ]; then
    systemctl --user enable asistif-gozluk
    echo "✓ Pi her açıldığında otomatik başlayacak"
fi
echo "Başlatmak için: systemctl --user start asistif-gozluk"
echo "Çıktıyı izlemek için: tail -f ~/Desktop/Gemını/gozluk.log"
