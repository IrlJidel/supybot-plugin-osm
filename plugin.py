###
# Copyright (c) 2012, Ian Dees
# All rights reserved.
#
#
###

from supybot.commands import *
import supybot.ircmsgs as ircmsgs
import supybot.callbacks as callbacks
import supybot.schedule as schedule
import supybot.world as world
import supybot.log as log

import traceback

import gzip
import StringIO
import itertools
import datetime
import calendar
import urllib2
import urllib
import xml.etree.cElementTree as ElementTree
import os
import json
import time
import re


stathat = None
try:
    from stathat import StatHat
    stathat = StatHat()
except:
    pass

class OscHandler():
    def __init__(self):
        self.changes = {}
        self.nodes = {}
        self.ways = {}
        self.relations = {}
        self.action = ""
        self.primitive = {}
        self.missingNds = set()

    def startElement(self, name, attributes):
        if name in ('modify', 'delete', 'create'):
            self.action = name
        if name in ('node', 'way', 'relation'):
            self.primitive['type'] = name
            self.primitive['id'] = int(attributes['id'])
            self.primitive['version'] = int(attributes['version'])
            self.primitive['changeset'] = int(attributes['changeset'])
            self.primitive['uid'] = int(attributes.get('uid'))
            self.primitive['user'] = attributes.get('user').encode('utf-8')
            self.primitive['timestamp'] = isoToDatetime(attributes['timestamp'])
            self.primitive['tags'] = {}
            self.primitive['action'] = self.action
        if name == 'node':
            self.primitive['lat'] = float(attributes['lat'])
            self.primitive['lon'] = float(attributes['lon'])
        elif name == 'tag':
            key = attributes['k'].encode('utf-8')
            val = attributes['v'].encode('utf-8')
            self.primitive['tags'][key] = val
        elif name == 'way':
            self.primitive['nodes'] = []
        elif name == 'relation':
            self.primitive['members'] = []
        elif name == 'nd':
            ref = int(attributes['ref'])
            self.primitive['nodes'].append(ref)
            if ref not in self.nodes:
                self.missingNds.add(ref)
        elif name == 'member':
            self.primitive['members'].append(
                                    {
                                     'type': attributes['type'],
                                     'role': attributes['role'].encode('utf-8'),
                                     'ref': attributes['ref']
                                    })

    def endElement(self, name):
        if name == 'node':
            self.nodes[self.primitive['id']] = self.primitive
        elif name == 'way':
            self.ways[self.primitive['id']] = self.primitive
        elif name == 'relation':
            self.relations[self.primitive['id']] = self.primitive
        if name in ('node', 'way', 'relation'):
            self.primitive = {}


def isoToDatetime(isotime):
    return datetime.datetime.strptime(isotime, "%Y-%m-%dT%H:%M:%SZ")


def prettyDate(d):
    diff = datetime.datetime.utcnow() - d
    s = diff.seconds
    if diff.days > 7 or diff.days < 0:
        return 'on %s' % (d.strftime('%d %b %Y'))
    elif diff.days == 1:
        return '1 day ago'
    elif diff.days > 1:
        return '{0} days ago'.format(diff.days)
    elif s <= 1:
        return 'just now'
    elif s < 60:
        return '{0} seconds ago'.format(s)
    elif s < 120:
        return '1 minute ago'
    elif s < 3600:
        return '{0} minutes ago'.format(s/60)
    elif s < 7200:
        return '1 hour ago'
    else:
        return '{0} hours ago'.format(s/3600)


def parseOsm(source, handler):
    for event, elem in ElementTree.iterparse(source, events=('start', 'end')):
        if event == 'start':
            handler.startElement(elem.tag, elem.attrib)
        elif event == 'end':
            handler.endElement(elem.tag)
        elem.clear()

_new_uid_edit_region_channels = {
    "#osm-ie":   ("Northern Ireland", "ie", ),
    "#osmiebot": ("Northern Ireland", "ie", ),
}

_note_edit_region_channels = {
    "#osm-ie":   ("Northern Ireland", "ie", ),
    "#osmiebot": ("Northern Ireland", "ie", ),
}

_note_cleaning_re = re.compile("\s+", flags=re.UNICODE)


class OSM(callbacks.Plugin):
    """Add the help for "@plugin help OSM" here
    This should describe *how* to use this plugin."""
    threaded = True

    def __init__(self, irc):
        self.__parent__ = super(OSM, self)
        self.__parent__.__init__(irc)
        self.seen_changesets = {}
        self.watch_users = [] 
        self.irc = irc
        #load watched users
        if os.path.exists('watchedusers.txt'):
            log.info('Loading watched users')
            with open('watchedusers.txt') as f:
                self.watch_users = f.read().splitlines()
            log.info("Watching %s" % (', '.join(self.watch_users)))
        self._start_polling()

    def die(self):
        self._stop_polling()
        self.__parent__.die()

    def _start_polling(self):
        log.info('Start polling.')
        schedule.addPeriodicEvent(self._minutely_diff_poll, 300, now=True, name='minutely_poll')
        schedule.addPeriodicEvent(self._notes_rss_poll, 60, now=True, name='notes_rss_poll')

    def _stop_polling(self):
        log.info('Stop polling.')
        schedule.removeEvent('minutely_poll')
        schedule.removeEvent('notes_rss_poll')

    def readState(self, filename):
        # Read the state.txt
        sf = open(filename, 'r')

        state = {}
        for line in sf:
            if line[0] == '#':
                continue
            (k, v) = line.split('=')
            state[k] = v.strip().replace("\\:", ":")

        sf.close()

        return state

    def fetchNextState(self, currentState):
        stateTs = datetime.datetime.strptime(currentState['timestamp'], "%Y-%m-%dT%H:%M:%SZ")
        nextTs = stateTs + datetime.timedelta(minutes=1)

        if datetime.datetime.utcnow() < nextTs:
            # The next timestamp is in the future, so don't try to get it.
            return False

        # Download the next state file
        nextSqn = int(currentState['sequenceNumber']) + 1
        sqnStr = str(nextSqn).zfill(9)
        #url = "http://planet.openstreetmap.org/replication/minute/%s/%s/%s.state.txt" % (sqnStr[0:3], sqnStr[3:6], sqnStr[6:9])
        url = "http://planet.openstreetmap.ie/replication/15minute/%s/%s/%s.state.txt" % (sqnStr[0:3], sqnStr[3:6], sqnStr[6:9])
        try:
            u = urllib2.urlopen(url)
            statefile = open('state.txt', 'w')
            statefile.write(u.read())
            statefile.close()
        except Exception, e:
            log.error("Error getting statefile from %s: %s" % (url,e))
            return False

        return True

    def reverse_geocode(self, lat, lon):
        url = 'http://nominatim.openstreetmap.ie/reverse?format=json&lat=%s&lon=%s' % (lat, lon)
        log.info("Requesting %s" % url)
        req = urllib2.Request(url)
        req.add_header('User-agent', 'osmie-bot')
        urldata = urllib2.urlopen(req)

        location = ""
        country_code = None
        state = None
        info = json.load(urldata)
        if 'address' in info:
            address = info.get('address')

            country_code = address.get('country_code')
            # Northern Ireland has state, Ireland doesn't
            if 'state' in address:
                state = address.get('state')

            # build location address from county downwards
            if 'county' in address:
                location = address.get('county')
            if 'administrative' in address:
                location = "%s, %s" % (address.get('administrative'), location)
            if 'city' in address:
                location = "%s, %s" % (address.get('city'), location)
            if 'municipal_district' in address:
                location = "%s, %s" % (address.get('municipal_district'), location)
            if 'town' in address:
                location = "%s, %s" % (address.get('town'), location)
            if 'village' in address:
                location = "%s, %s" % (address.get('village'), location)
            if 'suburb' in address:
                location = "%s, %s" % (address.get('suburb'), location)
            if 'hamlet' in address:
                location = "%s, %s" % (address.get('hamlet'), location)
            if 'locality' in address:
                location = "%s, %s" % (address.get('locality'), location)
            if 'road' in address:
                location = "%s, %s" % (address.get('road'), location)

            location = " near %s" % (location)
            location = location.encode('utf-8')

        if state is not None:
            return (state, location)
        else: 
            return (country_code, location)

    def _notes_rss_poll(self):
        url_templ = 'http://api.openstreetmap.org/api/0.6/notes/%d.json'
        short_text_len = 64

        try:
            if not os.path.exists('notes_state.txt'):
                log.error("No notes_state file found to poll note feed.")
                return

            notes_state = self.readState('notes_state.txt')
            log.info('Note state is %s' % json.dumps(notes_state))
            last_note_id = int(notes_state.get('last_note_id', None))
            last_note_time = isoToDatetime(notes_state.get('last_note_timestamp', ''))

            while True:
                last_note_id += 1
                url = url_templ % last_note_id
                log.info("Requesting %s" % url)
                try:
                    result = urllib2.urlopen(url)
                    note = json.load(result)
                    attrs = note.get('properties')
                    if len(attrs['comments']) > 0:
                        opening_comment = attrs['comments'][0]
                        author = opening_comment['user'].encode('utf-8') if 'user' in opening_comment else 'Anonymous'
                        full_text = _note_cleaning_re.sub(' ', opening_comment['text'])
                        short_text = ((full_text[:short_text_len-1] + u'\u2026') if len(full_text) > short_text_len else full_text).encode('utf-8')
                    else:
                        author = "Unknown"
                        short_text = "-No comment specified-"
                    date_created = datetime.datetime.strptime(attrs['date_created'], "%Y-%m-%d %H:%M:%S %Z")
                    geo = note.get('geometry').get('coordinates')
                    link = 'http://osm.org/note/%d' % last_note_id
                    location = ""
                    country_code = None

                    if stathat:
                        stathat.ez_post_count('donal.diamond@gmail.com', 'new notes', 1, date_created.isoformat()+'Z')

                    last_note_time = date_created

                    if (datetime.datetime.utcnow() - last_note_time).total_seconds() < 3600:
                        # Only reverse-geocode for newer notes
                        try:
                            country_code, location = self.reverse_geocode(geo[1], geo[0])
                        except urllib2.HTTPError as e:
                            log.error("HTTP problem when looking for note location: %s" % (e))

                    response = '%s posted a new note%s %s ("%s")' % (author, location, link, short_text)
                    irc = world.ircs[0]
                    for chan in irc.state.channels:
                        if country_code in _note_edit_region_channels.get(chan, ()):
                            log.info("%s" % response)
                            msg = ircmsgs.privmsg(chan, response)
                            world.ircs[0].queueMsg(msg)
                except urllib2.URLError, e:
                    if e.code == 404:
                        log.info("%s doesn't exist. Stopping." % last_note_id)
                        last_note_id -= 1

                        if (datetime.datetime.utcnow() - last_note_time).total_seconds() > 7200:
                            msg = ircmsgs.privmsg('IrlJidel_w', "No new notes since %s. Last note %d." % prettyDate(last_note_time), last_note_id )
                            world.ircs[0].queueMsg(msg)

                        break

            with open('notes_state.txt', 'w') as f:
                f.write('last_note_id=%s\n' % last_note_id)
                f.write('last_note_timestamp=%sZ\n' % last_note_time.isoformat())

        except Exception as e:
            log.error("Exception processing new notes: %s" % traceback.format_exc(e))

    def _minutely_diff_poll(self):
        try:
            if not os.path.exists('state.txt'):
                log.error("No state file found to poll minutelies.")
                return

            seen_uids = {}

            seen_changesets = self.seen_changesets

            state = self.readState('state.txt')

            while self.fetchNextState(state):
                state = self.readState('state.txt')

                # Grab the next sequence number and build a URL out of it
                sqnStr = state['sequenceNumber'].zfill(9)
                #url = "http://planet.openstreetmap.org/replication/minute/%s/%s/%s.osc.gz" % (sqnStr[0:3], sqnStr[3:6], sqnStr[6:9])
                url = "http://planet.openstreetmap.ie/replication/15minute/%s/%s/%s.osc.gz" % (sqnStr[0:3], sqnStr[3:6], sqnStr[6:9])

                log.info("Downloading change file (%s)." % (url))
                content = urllib2.urlopen(url)
                content = StringIO.StringIO(content.read())
                gzipper = gzip.GzipFile(fileobj=content)

                handler = OscHandler()
                parseOsm(gzipper, handler)

                for (id, prim) in itertools.chain(handler.nodes.iteritems(), handler.ways.iteritems(), handler.relations.iteritems()):

                    changeset_id = str(prim['changeset'])
                    action = prim['action']
                    prim_type = prim['type']

                    changeset_data = seen_changesets.get(changeset_id, {})
                    cs_type_data = changeset_data.get(prim_type, {})
                    cs_type_data[action] = cs_type_data.get(action, 0) + 1
                    cs_type_data['total_changes'] = cs_type_data.get('total_changes', 0) + 1
                    changeset_data[prim_type] = cs_type_data
                    changeset_data['total_changes'] = changeset_data.get('total_changes', 0) + 1
                    changeset_data['last_modified'] = prim['timestamp']
                    seen_changesets[changeset_id] = changeset_data

                    uid = str(prim['uid'])
                    if uid in seen_uids:
                        continue
                    else:
                        seen_uids[str(prim['uid'])] = {'changeset': prim['changeset'],
                                                       'username': prim['user']}

                    if 'lat' in prim and 'lat' not in seen_uids[str(prim['uid'])]:
                        seen_uids[str(prim['uid'])]['lat'] = prim['lat']
                        seen_uids[str(prim['uid'])]['lon'] = prim['lon']

                #log.info("Changeset actions: %s" % json.dumps(seen_changesets))

                # Check the changesets for anomolies
                now = datetime.datetime.utcnow()
                cs_flags = []
                for (id, cs_data) in seen_changesets.items():
                    age = (now - cs_data['last_modified']).total_seconds()
                    if age > 3600:
                        del seen_changesets[id]
                        continue

                    total_changes = cs_data['total_changes']
                    node_changes = cs_data.get('node', {}).get('total_changes', 0)
                    way_changes = cs_data.get('way', {}).get('total_changes', 0)
                    relation_changes = cs_data.get('relation', {}).get('total_changes', 0)
                    node_pct = node_changes / float(total_changes)
                    way_pct = way_changes / float(total_changes)
                    relation_pct = relation_changes / float(total_changes)

                    # Flag a changeset that's big and made up of all one primitive type
                    if total_changes > 2000 and (node_pct > 0.97 or way_pct > 0.97 or relation_pct > 0.97):
                        cs_flags.append((id, "it is mostly changes to one data type"))

                    creates = cs_data.get('node', {}).get('create', 0) + cs_data.get('way', {}).get('create', 0) + cs_data.get('relation', {}).get('create', 0)
                    mods = cs_data.get('node', {}).get('modify', 0) + cs_data.get('way', {}).get('modify', 0) + cs_data.get('relation', {}).get('modify', 0)
                    deletes = cs_data.get('node', {}).get('delete', 0) + cs_data.get('way', {}).get('delete', 0) + cs_data.get('relation', {}).get('delete', 0)
                    create_pct = creates / float(total_changes)
                    mod_pct = mods / float(total_changes)
                    delete_pct = deletes / float(total_changes)

                    # Flag a changeset that's big and made up of only one change type
                    if total_changes > 2000 and (create_pct > 0.97 or mod_pct > 0.97 or delete_pct > 0.97):
                        cs_flags.append((id, "it is mostly creates, modifies, or deletes"))

                # Tell the channel about these problems
                irc = world.ircs[0]
                for (cs_id, reason) in cs_flags:
                    if cs_id in seen_changesets and seen_changesets[cs_id].get('alerted_already'):
                        continue

                    response = "Changeset %s is weird because %s. http://osm.org/changeset/%s" % (cs_id, reason, cs_id)

                    log.info(response)
                    for chan in irc.state.channels:
                        if chan == "#osmiebot":
                            msg = ircmsgs.privmsg(chan, response)
                            world.ircs[0].queueMsg(msg)
                    seen_changesets[cs_id]['alerted_already'] = True

            log.info("There were %s users editing this time." % len(seen_uids))
            if stathat:
                stathat.ez_post_value('donal.diamond@gmail.com', 'users editing this minute', len(seen_uids), state['timestamp'])

            f = open('uid.txt', 'r')
            for line in f:
                for uid in seen_uids.keys():
                    if uid in line and seen_uids[uid]['username'] not in self.watch_users:
                        seen_uids.pop(uid)
                        continue
                if len(seen_uids) == 0:
                    break
            f.close()

            if stathat:
                stathat.ez_post_value('donal.diamond@gmail.com', 'new users this minute', len(seen_uids), state['timestamp'])

            f = open('uid.txt', 'a')
            for (uid, data) in seen_uids.iteritems():
                if data['username'] not in self.watch_users:
                    f.write('%s\t%s\n' % (data['username'], uid))

                location = ""
                country_code = None
                if 'lat' in data:
                    try:
                        country_code, location = self.reverse_geocode(data['lat'], data['lon'])
                    except urllib2.HTTPError as e:
                        log.error("HTTP problem when looking for edit location: %s" % (e))
                # get changeset comment
                changeset = self.changeset_details(data['changeset'])
                if data['username'] not in self.watch_users:
                    response = "%s just started editing%s with changeset http://osm.org/changeset/%s with %s" % (data['username'], location, data['changeset'], changeset['tags'])
                else:
                    response = "Watched user %s editing%s with changeset http://osm.org/changeset/%s with %s" % (data['username'], location, data['changeset'], changeset['tags'])
 
                log.info(response)
                irc = world.ircs[0]
                for chan in irc.state.channels:
                    #TODO: watched users shoudl be per channel
                    if country_code in _new_uid_edit_region_channels.get(chan, ()):
                        msg = ircmsgs.privmsg(chan, response)
                        world.ircs[0].queueMsg(msg)

            f.close()

        except Exception as e:
            log.error("Exception processing new users: %s" % traceback.format_exc(e))

    def tagKeySortKey(self, key):
        ret = key.lower()
        if ret.startswith('name'):
            return 'AAAAA'
        elif ret.startswith('highway'):
            return 'AAAAB'
        elif ret.startswith('amenity'):
            return 'AAAAC'
        elif ret.startswith('shop'):
            return 'AAAAD'
        else:
            return ret

    def node(self, irc, msg, args, node_id):
        """<node_id>

        Shows information about the specified OSM node ID."""
        baseUrl = "http://osm.org"

        if not node_id:
            irc.error('You forgot to give me a node ID.')
            return

        try:
            xml = urllib2.urlopen('%s/api/0.6/node/%d' % (baseUrl, node_id))
        except urllib2.HTTPError as e:
            if e.code == 410:
                irc.reply('Node %s was deleted.' % (node_id))
            elif e.code == 404:
                irc.error('Node %s was not found.' % (node_id))
            else:
                irc.error('Could not reach server for node %s.' % (node_id))
            return

        tree = ElementTree.ElementTree(file=xml)
        node_element = tree.find("node")

        username = node_element.attrib['user']
        version = node_element.attrib['version']
        timestamp = isoToDatetime(node_element.attrib['timestamp'])

        tag_strings = []
        tag_elems = node_element.findall('tag')
        for tag_elem in tag_elems:
            k = tag_elem.get('k')
            v = tag_elem.get('v')
            tag_strings.append("%s=%s" % (k, v))

        tag_strings = sorted(tag_strings, key=self.tagKeySortKey)

        if len(tag_strings) == 0:
            tag_str = 'no tags.'
        elif len(tag_strings) == 1:
            tag_str = 'tag %s' % (', '.join(tag_strings))
        elif len(tag_strings) > 1:
            tag_str = 'tags %s' % (', '.join(tag_strings))

        response = "Node %s: version %s by %s edited %s and has %s http://osm.org/node/%s" % (node_id,
                                                                          version,
                                                                          username,
                                                                          prettyDate(timestamp),
                                                                          tag_str,
                                                                          node_id)

        irc.reply(response.encode('utf-8'))
    node = wrap(node, ['int'])

    def way(self, irc, msg, args, way_id):
        """<way_id>

        Shows information about the specified OSM way ID."""
        baseUrl = "http://osm.org"

        if not way_id:
            irc.error('You forgot to give me a way ID.')
            return

        try:
            xml = urllib2.urlopen('%s/api/0.6/way/%d' % (baseUrl, way_id))
        except urllib2.HTTPError as e:
            if e.code == 410:
                irc.reply('Way %s was deleted.' % (way_id))
            elif e.code == 404:
                irc.error('Way %s was not found.' % (way_id))
            else:
                irc.error('Could not reach server for way %s.' % (way_id))
            return

        tree = ElementTree.ElementTree(file=xml)
        way_element = tree.find('way')

        username = way_element.attrib['user']
        version = way_element.attrib['version']
        timestamp = isoToDatetime(way_element.attrib['timestamp'])

        tag_strings = []
        tag_elems = way_element.findall('tag')
        for tag_elem in tag_elems:
            k = tag_elem.attrib['k']
            v = tag_elem.attrib['v']
            tag_strings.append("%s=%s" % (k, v))

        tag_strings = sorted(tag_strings, key=self.tagKeySortKey)

        if len(tag_strings) == 0:
            tag_str = 'no tags.'
        elif len(tag_strings) == 1:
            tag_str = 'tag %s' % (', '.join(tag_strings))
        elif len(tag_strings) > 1:
            tag_str = 'tags %s' % (', '.join(tag_strings))

        nd_refs = way_element.findall('nd')
        nd_refs_str = "NO NODES"
        if len(nd_refs) == 1:
            nd_refs_str = "1 NODE"
        elif len(nd_refs) > 1:
            nd_refs_str = "%d nodes" % (len(nd_refs))

        response = "Way %s: version %s by %s edited %s with %s and %s http://osm.org/way/%s" % \
                (way_id, version, username, prettyDate(timestamp), nd_refs_str, tag_str, way_id)

        irc.reply(response.encode('utf-8'))
    way = wrap(way, ['int'])

    def relation(self, irc, msg, args, relation_id):
        """<relation_id>

        Shows information about the specified OSM relation ID."""
        baseUrl = "http://osm.org"

        if not relation_id:
            irc.error('You forgot to give me a relation ID.')
            return

        try:
            xml = urllib2.urlopen('%s/api/0.6/relation/%d' % (baseUrl, relation_id))
        except urllib2.HTTPError as e:
            if e.code == 410:
                irc.reply('Relation %s was deleted.' % (relation_id))
            elif e.code == 404:
                irc.error('Relation %s was not found.' % (relation_id))
            else:
                irc.error('Could not reach server for relation %s.' % (relation_id))
            return

        tree = ElementTree.ElementTree(file=xml)
        relation_element = tree.find('relation')

        username = relation_element.attrib['user']
        version = relation_element.attrib['version']
        timestamp = isoToDatetime(relation_element.attrib['timestamp'])

        tag_strings = []
        tag_elems = relation_element.findall('tag')
        for tag_elem in tag_elems:
            k = tag_elem.attrib['k']
            v = tag_elem.attrib['v']
            tag_strings.append("%s=%s" % (k, v))

        tag_strings = sorted(tag_strings, key=self.tagKeySortKey)

        if len(tag_strings) == 0:
            tag_str = 'no tags.'
        elif len(tag_strings) == 1:
            tag_str = 'tag %s' % (', '.join(tag_strings))
        elif len(tag_strings) > 1:
            tag_str = 'tags %s' % (', '.join(tag_strings))

        members = relation_element.findall('member')
        members_str = "NO MEMBERS"
        if len(members) == 1:
            members_str = "1 member"
        elif len(members) > 1:
            members_str = "%d members" % (len(members))

        response = "Relation %s: version %s by %s edited %s with %s and %s http://osm.org/relation/%s" % \
                (relation_id, version, username, prettyDate(timestamp), members_str, tag_str, relation_id)

        irc.reply(response.encode('utf-8'))
    relation = wrap(relation, ['int'])

    def changeset(self, irc, msg, args, changeset_id):
        """<changeset_id>

        Shows information about the specified OSM changeset ID."""
        baseUrl = "http://osm.org"

        if not changeset_id:
            irc.error('You forgot to give me a changeset ID.')
            return


	changeset = self.changeset_details(changeset_id) 
	if not changeset:
	    irc.error('Changeset %s was not found.' % (changeset_id))
	    return

	response = "Changeset http://osm.org/changeset/%s by %s opened %s %s with %s" % \
		(changeset['id'], changeset['username'], changeset['created'], changeset['length'], changeset['tags'])


        irc.reply(response.encode('utf-8'))
    changeset = wrap(changeset, ['int'])

    def changeset_details(self, changeset_id):
	baseUrl = "http://osm.org"

	changeset = {}

	try:
	    xml = urllib2.urlopen('%s/api/0.6/changeset/%d' % (baseUrl, changeset_id))
	except urllib2.HTTPError as e:
	    return

	tree = ElementTree.ElementTree(file=xml)
	changeset_element = tree.find('changeset')

	username = changeset_element.attrib['user']
	currently_open = changeset_element.attrib['open']
	created = isoToDatetime(changeset_element.attrib['created_at'])

	if currently_open == 'true':
	    length_str = "(still open)"
	elif currently_open == 'false':
	    closed = isoToDatetime(changeset_element.attrib['closed_at'])
	    length_str = "open %s minutes" % ((closed - created).seconds / 60)

	tag_strings = []
	tag_elems = changeset_element.findall('tag')
	for tag_elem in tag_elems:
	    k = tag_elem.attrib['k']
	    v = tag_elem.attrib['v']
	    tag_strings.append("%s=%s" % (k, v))

	tag_strings = sorted(tag_strings, key=self.tagKeySortKey)

	if len(tag_strings) == 0:
	    tag_str = 'no tags.'
	elif len(tag_strings) == 1:
	    tag_str = 'tag %s' % (', '.join(tag_strings))
	elif len(tag_strings) > 1:
	    tag_str = 'tags %s' % (', '.join(tag_strings))
       
	changeset['id'] = changeset_id
	changeset['username'] = username
	changeset['created'] = prettyDate(created)
	changeset['length'] = length_str
	changeset['tags'] = tag_str

	return changeset


    def watch(self, irc, msg, args, username):
        """\"<username>\"

        Start reporting on edits for the given user."""
        baseUrl = "http://osm.org"

        if not username:
            irc.error('You forgot to give me a username.')
            return

        quoted_uname = username
        quoted_uname = urllib.quote(quoted_uname)

        try:
            xml = urllib2.urlopen('%s/user/%s/edits/feed' % (baseUrl, quoted_uname))
        except urllib2.HTTPError as e:
            response = "Username %s was not found." % (username)
            irc.reply(response.encode('utf-8'))
            return
        except Exception as e:
            irc.error("Could not parse the user's changeset feed.")
            log.error(traceback.format_exc(e))
            return

        if username in self.watch_users: 
            response = "We are already watching %s" % (username)
        else:
            self.watch_users.append(username)
            with open('watchedusers.txt', 'w') as f:
                f.write("\n".join(self.watch_users) + "\n")
            response = "We are now watching %s" % (username)

        irc.reply(response.encode('utf-8'))
    watch = wrap(watch, ['anything'])


    def unwatch(self, irc, msg, args, username):
        """\"<username>\"

        Stop reporting on edits for the given user."""

        if not username:
            irc.error('You forgot to give me a username.')
            return

        if username in self.watch_users: 
            self.watch_users.remove(username)
            with open('watchedusers.txt', 'w') as f:
                f.write("\n".join(self.watch_users) + "\n")
            response = "Stopped watching %s" % (username)
        else:
            response = "We weren't watching %s" % (username)

        irc.reply(response.encode('utf-8'))
    unwatch = wrap(unwatch, ['anything'])

    def watching(self, irc, msg, args):
        """Show list of users we are reporting edits for"""

        if not self.watch_users:
            response = "We are not watching anybody yet"
        else:
            response = "Watching %s" % (', '.join(self.watch_users))

        irc.reply(response.encode('utf-8'))
    watching = wrap(watching)

    def sing(self, irc, msg, args):
        """Sing bots national anthem"""

        lines = ( 
            "Every node you make...we'll be watching you...",
            "Every way you draw....",
            "Every relation you group....",
            "Every typo you make, every way you break",
            "Every tag you fake...",
            "Every way you glue...",
            "We'll be watching you",
            "Oh can't you see?",
            "The data belongs to we.",
            "That's why we QA.",
            "Every single way.", )

        for line in lines:
            response = line
            irc.reply(response.encode('utf-8'))
            time.sleep(0.5)

    sing = wrap(sing)

    def botsnack(self, irc, msg, args):
        """Give bot a snack"""
        
        response = "YUM!"
        irc.reply(response.encode('utf-8'))

    botsnack = wrap(botsnack)

    def last_edit(self, irc, msg, args, username):
        """\"<username>\"

        Shows information about the last edit for the given user."""
        baseUrl = "http://osm.org"

        if not username:
            irc.error('You forgot to give me a username.')
            return

        quoted_uname = username
        quoted_uname = urllib.quote(quoted_uname)

        try:
            xml = urllib2.urlopen('%s/user/%s/edits/feed' % (baseUrl, quoted_uname))
        except urllib2.HTTPError as e:
            irc.error('Username %s was not found.' % (username))
            return
        except Exception as e:
            irc.error("Could not parse the user's changeset feed.")
            log.error(traceback.format_exc(e))
            return

        tree = ElementTree.ElementTree(file=xml)
        first_entry = tree.find('{http://www.w3.org/2005/Atom}entry')

        if first_entry is None:
            irc.error("Looks like %s doesn't have any edits." % (username))
            return

        author = first_entry.findtext('{http://www.w3.org/2005/Atom}author/{http://www.w3.org/2005/Atom}name')
        timestamp = first_entry.findtext('{http://www.w3.org/2005/Atom}updated')
        entry_id = first_entry.findtext('{http://www.w3.org/2005/Atom}id')

        if author != username:
            # It looks like there's a bug where the API will give back the most recent user's edit feed
            # instead of a 404
            irc.error('Unknown username. Was "%s" but asked for "%s"' % (author, username))
            return

        # Strip off the word "Changeset " from the title to get the number
        changeset_id = entry_id[39:]
        #TODO: tags are already in the feed
        changeset = self.changeset_details(int(changeset_id)) 

        updated = isoToDatetime(timestamp)

        response = "User %s last edited %s with changeset http://osm.org/changeset/%s with %s" % (author, prettyDate(updated), changeset_id, changeset['tags'])

        irc.reply(response.encode('utf-8'))
    lastedit = wrap(last_edit, ['anything'])

    def taginfo(self, irc, msg, args, tag_query):
        """<tag key>[=<tag value>|*]

        Shows information about the specified tag key/value combination."""

        if not tag_query:
            irc.error('You forgot to give me a tag_query.')
            return

        k = None
        v = None
        if '=' in tag_query:
            (k,v) = tag_query.split('=')
            if '*' == v:
                v = None
        else:
            k = tag_query

        if k is None:
            irc.error("I don't know how to parse that key/value pair.")
            return
        else:
            self._taginfo_region("http://taginfo.openstreetmap.ie", "island of Ireland", irc, k, v)
            self._taginfo_region("http://taginfo.openstreetmap.org", "planet", irc, k, v)
    taginfo = wrap(taginfo, ['anything'])

    def _taginfo_region(self, baseUrl, region, irc, k, v=None):

        try:
            if v is None:
                j = urllib2.urlopen('%s/api/4/key/stats?key=%s' % (baseUrl, urllib.quote(k)), timeout=30.0)
                data = json.load(j)

                response = "Tag %s has %s values and appears %s times in the %s. %s/keys/%s" % (k, data['data'][0]['values'], data['data'][0]['count'], region, baseUrl, urllib.quote(k))
                
            else:
                j = urllib2.urlopen('%s/api/4/tag/stats?key=%s&value=%s' % (baseUrl, urllib.quote(k), urllib.quote(v)), timeout=30.0)
                data = json.load(j)

                response = "Tag %s=%s appears %s times in the %s. %s/tags/%s=%s" % (k, v, data['data'][0]['count'], region, baseUrl, urllib.quote(k), urllib.quote(v))
            irc.reply(response)
        except urllib2.URLError as e:
            irc.error("There was an error connecting to %s server. Try again later." % baseUrl)
            return

Class = OSM


# vim:set shiftwidth=4 softtabstop=4 expandtab textwidth=79:
