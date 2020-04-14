from ads_dejavu.database import get_database, Database
import ads_dejavu.decoder as decoder
import ads_dejavu.fingerprint as fingerprint
import multiprocessing
import os
import logging


class Dejavu(object):

    SONG_ID = "song_id"
    SONG_NAME = 'song_name'
    CONFIDENCE = 'confidence'
    MATCH_TIME = 'match_time'
    OFFSET = 'offset'
    OFFSET_SECS = 'offset_seconds'
    AUDIO_LENGTH = 'audio_length'
    RELATIVE_CONFIDENCE = 'relative_confidence'
    RELATIVE_CONFIDENCE2 = 'relative_confidence2'
    RELATIVE_CONFIDENCE3 = 'relative_confidence3'
    RELATIVE_CONFIDENCE4 = 'relative_confidence4'

    def __init__(self, config):
        super(Dejavu, self).__init__()

        self.config = config

        # initialize db
        db_cls = get_database(config.get("database_type", None))

        self.db = db_cls(**config.get("database", {}))
        self.db.setup()

        # if we should limit seconds fingerprinted,
        # None|-1 means use entire track
        self.limit = self.config.get("fingerprint_limit", None)
        if self.limit == -1:  # for JSON compatibility
            self.limit = None
        self.get_fingerprinted_songs()

    def get_fingerprinted_songs(self):
        # get songs previously indexed
        self.songs = self.db.get_songs()
        self.songhashes_set = set()  # to know which ones we've computed before
        for song in self.songs:
            song_hash = song[Database.FIELD_FILE_SHA1]
            self.songhashes_set.add(song_hash)

    def fingerprint_directory(self, path, extensions, nprocesses=None):
        # Try to use the maximum amount of processes if not given.
        try:
            nprocesses = nprocesses or multiprocessing.cpu_count()
        except NotImplementedError:
            nprocesses = 1
        else:
            nprocesses = 1 if nprocesses <= 0 else nprocesses

        pool = multiprocessing.Pool(nprocesses)

        filenames_to_fingerprint = []
        for filename, _ in decoder.find_files(path, extensions):

            # don't refingerprint already fingerprinted files
            if decoder.unique_hash(filename) in self.songhashes_set:
                logging.getLogger('dejavu').warn("%s already fingerprinted, continuing..." % filename)
                continue

            filenames_to_fingerprint.append(filename)

        # Prepare _fingerprint_worker input
        worker_input = zip(filenames_to_fingerprint,
                           [self.limit] * len(filenames_to_fingerprint))

        # Send off our tasks
        iterator = pool.imap_unordered(_fingerprint_worker,
                                       worker_input)

        # Loop till we have all of them
        while True:
            try:
                song_name, hashes, file_hash, audio_length = iterator.next()
            except multiprocessing.TimeoutError:
                continue
            except StopIteration:
                break
            except:
                logging.getLogger('dejavu').exception("Failed fingerprinting")
            else:
                logging.getLogger('dejavu').debug("Inserting " + song_name + " in database")
                sid = self.db.insert_song(song_name, file_hash, audio_length)

                self.db.insert_hashes(sid, set([(x[0], int(x[1])) for x in hashes]))
                self.db.set_song_fingerprinted(sid)
                self.get_fingerprinted_songs()
                logging.getLogger('dejavu').info(song_name + " inserted in database")
        pool.close()
        pool.join()

    def fingerprint_file(self, filepath, song_name=None):
        songname = decoder.path_to_songname(filepath)
        song_hash = decoder.unique_hash(filepath)
        song_name = song_name or songname
        # don't refingerprint already fingerprinted files
        if song_hash in self.songhashes_set:
            logging.getLogger('dejavu').warn("%s already fingerprinted, continuing..." % song_name)
        else:
            song_name, hashes, file_hash, audio_length = _fingerprint_worker(
                filepath,
                self.limit,
                song_name=song_name
            )
            logging.getLogger('dejavu').debug("Inserting " + song_name + " in database")
            sid = self.db.insert_song(song_name, file_hash, audio_length)

            self.db.insert_hashes(sid, set([(x[0], int(x[1])) for x in hashes]))
            self.db.set_song_fingerprinted(sid)
            self.get_fingerprinted_songs()
            logging.getLogger('dejavu').info(song_name + " inserted in database")

    def find_matches(self, samples, Fs=fingerprint.DEFAULT_FS):
        hashes = fingerprint.fingerprint(samples, Fs=Fs)
        mapper = {}
        total_hashes = 0
        for hash, offset in hashes:
            mapper[hash.upper()[:fingerprint.FINGERPRINT_REDUCTION]] = offset
            total_hashes += 1
        return (self.db.return_matches(mapper), total_hashes)

    def align_matches(self, matches, total_hashes, audio_len=-1):
        """
            Finds hash matches that align in time with other matches and finds
            consensus about which hashes are "true" signal from the audio.

            Returns a dictionary with match information.
        """
        # align by diffs
        diff_counter = {}
        largest = 0
        largest_count = 0
        song_id = -1
        for tup in matches:
            sid, diff = tup
            if diff not in diff_counter:
                diff_counter[diff] = {}
            if sid not in diff_counter[diff]:
                diff_counter[diff][sid] = 0
            diff_counter[diff][sid] += 1

            if diff_counter[diff][sid] > largest_count:
                largest = diff
                largest_count = diff_counter[diff][sid]
                song_id = sid

        # extract idenfication
        song = self.db.get_song_by_id(song_id)
        if song:
            # TODO: Clarify what `get_song_by_id` should return.
            songname = song.get(Dejavu.SONG_NAME, None)
        else:
            return None
        # total_hashes_of_sid = len(list(filter(lambda x: x[0] == sid, matches)))
        # return match info
        nseconds = round(float(largest) / fingerprint.DEFAULT_FS *
                         fingerprint.DEFAULT_WINDOW_SIZE *
                         fingerprint.DEFAULT_OVERLAP_RATIO, 5)
        database_audio_len = song.get(Database.AUDIO_LENGTH, None)
        len_ratio = database_audio_len / audio_len if database_audio_len > audio_len else 1
        song = {
            Dejavu.SONG_ID : song_id,
            Dejavu.SONG_NAME : songname,
            Dejavu.CONFIDENCE : largest_count,
            Dejavu.AUDIO_LENGTH : database_audio_len,
            Dejavu.RELATIVE_CONFIDENCE: (largest_count * len_ratio * 100) / song['num_fingerprints'],
            Dejavu.RELATIVE_CONFIDENCE2: (largest_count * 100) / float(total_hashes),
            Dejavu.RELATIVE_CONFIDENCE3: (largest_count * 100) / song['num_fingerprints'],
            #Dejavu.RELATIVE_CONFIDENCE3: (((largest_count*100)/song['num_fingerprints']) + ((largest_count*100)/float(total_hashes)))/2,
            Dejavu.RELATIVE_CONFIDENCE4: (largest_count * 100) / ((float(total_hashes)+song['num_fingerprints'])/2),
            Dejavu.OFFSET : int(largest),
            Dejavu.OFFSET_SECS : nseconds,
            Database.FIELD_FILE_SHA1 : song.get(Database.FIELD_FILE_SHA1, None),
        }
        return song

    def recognize(self, recognizer, *options, **kwoptions):
        r = recognizer(self)
        return r.recognize(*options, **kwoptions)


def _fingerprint_worker(filename, limit=None, song_name=None):
    # Pool.imap sends arguments as tuples so we have to unpack
    # them ourself.
    try:
        filename, limit = filename
    except ValueError:
        pass

    songname, extension = os.path.splitext(os.path.basename(filename))
    song_name = song_name or songname
    channels, Fs, file_hash, audio_length = decoder.read(filename, limit)
    result = set()
    channel_amount = len(channels)

    for channeln, channel in enumerate(channels):
        logging.getLogger('dejavu').info("Fingerprinting channel %d/%d for %s" % (channeln + 1, channel_amount, filename))
        hashes = fingerprint.fingerprint(channel, Fs=Fs)
        logging.getLogger('dejavu').debug("Finished channel %d/%d for %s" % (channeln + 1, channel_amount, filename))
        result |= set(hashes)

    return song_name, result, file_hash, audio_length


def chunkify(lst, n):
    """
    Splits a list into roughly n equal parts.
    http://stackoverflow.com/questions/2130016/splitting-a-list-of-arbitrary-size-into-only-roughly-n-equal-parts
    """
    return [lst[i::n] for i in xrange(n)]
