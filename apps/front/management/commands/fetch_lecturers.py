import re
import csv
import requests
import sys
import getpass
from BeautifulSoup import BeautifulSoup
from StringIO import StringIO
from collections import namedtuple
from optparse import make_option
from django.core.management.base import BaseCommand
from django.core.exceptions import ObjectDoesNotExist
from apps.front.models import Lecturer


def unicode_csv_reader(utf8_data, dialect=csv.excel, **kwargs):
    csv_reader = csv.reader(utf8_data, dialect=dialect, **kwargs)
    for row in csv_reader:
        yield [unicode(cell, 'latin1') for cell in row]


class HsrWebsite(object):
    """Class to coordinate access to the HSR website."""
    base_url = u'https://www.hsr.ch/index.php'
    
    def __init__(self):
        """Initialize requests session."""
        self.s = requests.session()

    def logged_in(self):
        return '.hsr.ch' in self.s.cookies._cookies \
                or 'www.hsr.ch' in self.s.cookies._cookies

    def login(self, username, password):
        """Login to the internal part of the HSR site."""
        if self.logged_in():
            return
        self.s.post(self.base_url, params={'id': 4409}, data={
            'logintype': 'login',
            'user': username,
            'pass': password,
            'pid': '4394',
            'submit': 'Login',
        })
    
    def get_person_id(self, first_name, last_name, room=None):
        """Search for a person on the HSR page and return id."""
        assert self.logged_in(), 'Not logged in. Please call login().'
        r = self.s.get(self.base_url, params={
            'id': 2116,
            'view': 'viewAdvanceSearch',
            'no_cache': 1,
            'surname': last_name,
            'firstname': first_name,
        })
        r.raise_for_status()  # Raise exception if request fails
        soup = BeautifulSoup(r.content)
        table = soup.find('table', attrs={'id': 'tx_icscrm_table'})
        rows = table.findAll('tr', recursive=False)
        # Find matching row. Compare name and - if provided - room number.
        for row in rows:
            cols = row.findAll('td') 
            if not cols:
                raise RuntimeError('Person not found.')
            fullname = u'%s&nbsp;%s' % (last_name, first_name)
            if cols[0].text.lower() != fullname.lower():
                continue
            if room is not None:
                # If room is an empty string, replace it with &nbsp;
                if room == '':
                    room = '&nbsp;'
                if cols[2].text != room:
                    continue
            return int(re.sub(r'^.*=(\d+)\'$', r'\1', row['onclick']))
        raise RuntimeError('Could not find person id.')
    
    def get_persons_csv(self):
        """Fetch persons csv file."""
        assert self.logged_in(), 'Not logged in. Please call login().'
        r = self.s.get(self.base_url, params={
            'id': 2116,
            'no_cache': 1,
            'view': 'viewTotalExcel',
        })
        r.raise_for_status()  # Raise exception if request fails
        return StringIO(r.content)


class Command(BaseCommand):
    help = 'Fetch lecturers and write them into the database.'
    option_list = BaseCommand.option_list + (
        make_option('--user',
            dest='username', help='HSR username'),
        make_option('--pass',
            dest='password', help='HSR password'),
        )

    def printO(self, msg):
        """Print to stdout. This expects unicode strings!"""
        encoding = self.stdout.encoding or sys.getdefaultencoding()
        self.stdout.write(msg.encode(encoding, 'replace'))

    def printE(self, msg, newline=True):
        """Print to stderr. This expects unicode strings!"""
        encoding = self.stderr.encoding or sys.getdefaultencoding()
        self.stderr.write(msg.encode(encoding, 'replace'))
        if newline:
            self.stdout.write('\n')

    def handle(self, **options):
        assert 'username' in options, '--user argument required'
        assert options['username'], '--user argument required'

        if not ('password' in options and options['password']):
            options['password'] = getpass.getpass('HSR Password: ').strip()

        # Initialize counters
        parsed_count = 0
        added_count = 0
        skipped_count = 0
        updated_count = 0

        # Initialize HsrWebsite object to fetch person IDs
        hsr = HsrWebsite()
        hsr.login(options['username'], options['password'])

        # Read and parse CSV
        f = hsr.get_persons_csv()
        reader = unicode_csv_reader(f, delimiter=';')
        # The last element added to the list is a little hack, because all
        # rows except the title row have a trailing semicolon.
        titles = [t.lower().replace(' ', '_') for t in reader.next()] + ['empty']
        Person = namedtuple('Person', titles)
        for p in map(Person._make, reader):
            parsed_count += 1
            if not p.initialen:
                self.printO(u'SKIP: %s, %s' % (p.name, p.vorname))
                skipped_count += 1
                continue  # Don't add people without an abbreviation
            hsr_id = hsr.get_person_id(p.vorname, p.name, p.raum)
            l, created = Lecturer.objects.get_or_create(pk=hsr_id)
            added_count += int(created)
            try:
                others = Lecturer.objects.get(abbreviation=p.initialen)
            except ObjectDoesNotExist:
                l.abbreviation = p.initialen
            else:
                if others.pk != hsr_id:
                    self.printO(u'WARNING: Added an index to pre-existing abbreviation %s'
                                                                                  % p.initialen)
                    l.abbreviation = u'%s2' % p.initialen
                else:
                    l.abbreviation = p.initialen
            l.title = p.titel
            l.first_name = p.vorname
            l.last_name = p.name
            l.department = p.abteilung
            l.function = p.funktion
            l.main_area = p.fachschwerpunkt
            l.email = p.email
            l.office = p.raum
            l.save()
            if created:
                self.printO(u'ADD: %s' % l.name())
            else:
                updated_count += 1
                self.printO(u'UPDATE: %s' % l.name())
        f.close()

        self.printO(u'\nParsed %u lecturers.' % parsed_count)
        self.printO(u'Added %u lecturers.' % added_count)
        self.printO(u'Updated %u lecturers.' % updated_count)
        self.printO(u'Skipped %u lecturers.' % skipped_count)
