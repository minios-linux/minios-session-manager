#!/usr/bin/make -f

# MiniOS Session Manager Makefile

PREFIX ?= /usr
BINDIR = $(PREFIX)/bin
LIBDIR = $(PREFIX)/lib/minios-session-manager
SHAREDIR = $(PREFIX)/share
LOCALEDIR = $(SHAREDIR)/locale

INSTALL = install
INSTALL_PROGRAM = $(INSTALL) -m 755
INSTALL_DATA = $(INSTALL) -m 644
INSTALL_DIR = $(INSTALL) -d

.PHONY: all install install-bin install-lib install-share install-locale clean build-deb

all:
	@echo "MiniOS Session Manager"
	@echo "Available targets:"
	@echo "  install      - Install to system"
	@echo "  install-bin  - Install binaries only"
	@echo "  install-lib  - Install libraries only" 
	@echo "  install-share - Install shared files only"
	@echo "  build-deb    - Build Debian package"
	@echo "  clean        - Clean build files"

install: install-bin install-lib install-share install-locale

install-bin:
	$(INSTALL_DIR) $(DESTDIR)$(BINDIR)
	$(INSTALL_PROGRAM) bin/minios-session-manager $(DESTDIR)$(BINDIR)/
	$(INSTALL_PROGRAM) bin/minios-session-cli $(DESTDIR)$(BINDIR)/

install-lib:
	$(INSTALL_DIR) $(DESTDIR)$(LIBDIR)
	$(INSTALL_DATA) lib/session_manager.py $(DESTDIR)$(LIBDIR)/
	$(INSTALL_DATA) lib/session_cli.py $(DESTDIR)$(LIBDIR)/
	$(INSTALL_PROGRAM) lib/session_cli_privileged.py $(DESTDIR)$(LIBDIR)/

install-share:
	$(INSTALL_DIR) $(DESTDIR)$(SHAREDIR)/applications
	$(INSTALL_DIR) $(DESTDIR)$(SHAREDIR)/polkit-1/actions
	$(INSTALL_DATA) share/applications/minios-session-manager.desktop $(DESTDIR)$(SHAREDIR)/applications/
	$(INSTALL_DATA) share/polkit/dev.minios.session-manager.policy $(DESTDIR)$(SHAREDIR)/polkit-1/actions/

install-locale:
	@if [ -d po/ ]; then \
		for po in po/*.po; do \
			if [ -f "$$po" ]; then \
				lang=$$(basename $$po .po); \
				$(INSTALL_DIR) $(DESTDIR)$(LOCALEDIR)/$$lang/LC_MESSAGES; \
				msgfmt $$po -o $(DESTDIR)$(LOCALEDIR)/$$lang/LC_MESSAGES/minios-session-manager.mo; \
			fi; \
		done; \
	fi

build-deb:
	dpkg-buildpackage -us -uc -b

clean:
	rm -rf debian/minios-session-manager/
	rm -f debian/files
	rm -f debian/debhelper-build-stamp
	rm -f debian/*.log
	rm -f debian/*.substvars
	find . -name "*.pyc" -delete
	find . -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true

uninstall:
	rm -f $(DESTDIR)$(BINDIR)/minios-session-manager
	rm -f $(DESTDIR)$(BINDIR)/minios-session-cli
	rm -rf $(DESTDIR)$(LIBDIR)
	rm -f $(DESTDIR)$(SHAREDIR)/applications/minios-session-manager.desktop
	rm -f $(DESTDIR)$(SHAREDIR)/polkit-1/actions/dev.minios.session-manager.policy
	# Remove locale files
	find $(DESTDIR)$(LOCALEDIR) -name "minios-session-manager.mo" -delete 2>/dev/null || true