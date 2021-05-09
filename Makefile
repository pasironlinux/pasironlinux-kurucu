DESTDIR=/

all: clean build

build:
	mkdir -p build/usr/lib/ || true
	mkdir -p build/usr/bin/ || true
	mkdir -p build/usr/share/applications || true
	cp -prfv live-installer build/usr/lib/
	install data/live-installer.sh build/usr/bin/live-installer
	install data/live-installer.desktop build/usr/share/applications/live-installer.desktop
	mkdir -p build/usr/lib/live-installer/scripts
	install data/preinstall.sh build/usr/lib/live-installer/scripts/preinstall.sh
	install data/postinstall.sh build/usr/lib/live-installer/scripts/postinstall.sh
	
	#set parmissions
	chmod 755 -R build
	chown root -R build
	
install:
	cp -prfv build/* $(DESTDIR)/
uninstall:
	rm -rf $(DESTDIR)/usr/lib/live-installer
	rm -f $(DESTDIR)/usr/bin/live-installer
	rm -f $(DESTDIR)/usr/share/applications/live-installer.desktop
clean:
	rm -rf build
