EXECUTABLES = bin/minios-session bin/minios-session-manager
LIBRARIES = lib/*.py
APPLICATIONS = share/applications/minios-session-manager.desktop
POLICIES = share/polkit/dev.minios.session-manager.policy
STYLES = share/styles/style.css

BINDIR = usr/bin
LIBDIR = usr/lib/minios-session-manager
APPLICATIONSDIR = usr/share/applications
POLKITACTIONSDIR = usr/share/polkit-1/actions
LOCALEDIR = usr/share/locale
SHAREDIR = usr/share/minios-session-manager

PO_FILES = $(shell find po -maxdepth 1 -name "*.po")
MO_FILES = $(patsubst %.po,%.mo,$(PO_FILES))

build: mo

mo: $(MO_FILES)

update-po:
	@echo "Updating translation files..."
	./update-po.sh

%.mo: %.po
	@echo "Generating mo file for $<"
	msgfmt -o $@ $<
	chmod 644 $@

clean:
	rm -rf $(MO_FILES)

install: build
	install -d $(DESTDIR)/$(BINDIR) \
				$(DESTDIR)/$(LIBDIR) \
				$(DESTDIR)/$(APPLICATIONSDIR) \
				$(DESTDIR)/$(POLKITACTIONSDIR) \
				$(DESTDIR)/$(LOCALEDIR) \
				$(DESTDIR)/$(SHAREDIR) \
				$(DESTDIR)/$(SHAREDIR)/styles

	cp $(EXECUTABLES) $(DESTDIR)/$(BINDIR)/
	cp $(LIBRARIES) $(DESTDIR)/$(LIBDIR)/
	chmod +x $(DESTDIR)/$(LIBDIR)/minios_session.py
	chmod +x $(DESTDIR)/$(LIBDIR)/minios_session_manager.py
	cp $(APPLICATIONS) $(DESTDIR)/$(APPLICATIONSDIR)
	cp $(POLICIES) $(DESTDIR)/$(POLKITACTIONSDIR)
	cp $(STYLES) $(DESTDIR)/$(SHAREDIR)/

	@for MO_FILE in $(MO_FILES); do \
		LOCALE=$$(basename $$MO_FILE .mo); \
		echo "Copying mo file $$MO_FILE to $(DESTDIR)/usr/share/locale/$$LOCALE/LC_MESSAGES/minios-session-manager.mo"; \
		install -Dm644 "$$MO_FILE" "$(DESTDIR)/usr/share/locale/$$LOCALE/LC_MESSAGES/minios-session-manager.mo"; \
	done

uninstall:
	@echo "Uninstalling MiniOS Session Manager..."
	
	# Remove executables
	rm -f $(DESTDIR)/$(BINDIR)/minios-session
	rm -f $(DESTDIR)/$(BINDIR)/minios-session-manager
	
	# Remove library directory
	rm -rf $(DESTDIR)/$(LIBDIR)
	
	# Remove desktop file
	rm -f $(DESTDIR)/$(APPLICATIONSDIR)/minios-session-manager.desktop
	
	# Remove PolicyKit policy
	rm -f $(DESTDIR)/$(POLKITACTIONSDIR)/dev.minios.session-manager.policy
	
	# Remove shared directory
	rm -rf $(DESTDIR)/$(SHAREDIR)
	
	# Remove translations
	@for MO_FILE in $(MO_FILES); do \
		LOCALE=$$(basename $$MO_FILE .mo); \
		echo "Removing translation file for locale $$LOCALE"; \
		rm -f "$(DESTDIR)/usr/share/locale/$$LOCALE/LC_MESSAGES/minios-session-manager.mo"; \
		rmdir "$(DESTDIR)/usr/share/locale/$$LOCALE/LC_MESSAGES" 2>/dev/null || true; \
		rmdir "$(DESTDIR)/usr/share/locale/$$LOCALE" 2>/dev/null || true; \
	done
	
	# Remove man pages (if installed by debhelper)
	rm -f $(DESTDIR)/usr/share/man/man1/minios-session.1*
	rm -f $(DESTDIR)/usr/share/man/man1/minios-session-manager.1*

	@echo "MiniOS Session Manager uninstalled successfully"

reinstall: uninstall install
	@echo "MiniOS Session Manager reinstalled successfully"

help:
	@echo "MiniOS Session Manager - Available targets:"
	@echo ""
	@echo "  build       - Build translation files (.mo)"
	@echo "  clean       - Remove built files (.mo)"
	@echo "  install     - Install to DESTDIR (default: /)"
	@echo "  uninstall   - Remove installed files from DESTDIR"
	@echo "  reinstall   - Uninstall and install again"
	@echo "  update-po   - Update translation template and files"
	@echo "  help        - Show this help message"
	@echo ""
	@echo "Variables:"
	@echo "  DESTDIR     - Installation prefix (default: /)"
	@echo ""
	@echo "Examples:"
	@echo "  make install DESTDIR=/tmp/test    # Install to test directory"
	@echo "  make uninstall                    # Remove from system"
	@echo "  sudo make reinstall               # Reinstall as root"

.PHONY: build mo update-po clean install uninstall reinstall help
