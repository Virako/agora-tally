from __future__ import unicode_literals
import random
import copy
import uuid
import sys
import codecs
import os
import tempfile

from openstv.ballots import Ballots
from openstv.plugins import getMethodPlugins

from .base import BaseVotingSystem, BaseTally, BlankVoteException

class Approval(BaseVotingSystem):
    '''
    Defines the helper functions that allows agora to manage an OpenSTV-based
    Approval voting system.
    '''

    @staticmethod
    def get_id():
        '''
        Returns the identifier of the voting system, used internally to
        discriminate  the voting system used in an election
        '''
        return 'APPROVAL'

    @staticmethod
    def get_description():
        return _('Multi-seat approval voting')

    @staticmethod
    def create_tally(election, question_num):
        '''
        Create object that helps to compute the tally
        '''
        return ApprovalTally(election, question_num)

class ApprovalTally(BaseTally):
    '''
    Class used to tally an election
    '''
    ballots_file = None
    ballots_path = ""

    # list containing the current list of ballots.
    # In each iteration this list is modified. For efficiency, ballots with the
    # same ordered choices are grouped. The format of each item in this list is
    # the following:
    #
    #{
        #'votes': 12, # number of ballots with this selection of choices
        #'answers': [2, 1, 4] # list of ids of the choices
    #}
    ballots = []

    # dict that has as keys the possible answer['value'], and as value the id
    # of each answer.
    # Used because internally we store the answers by id with a number to speed
    # things up.
    answer_to_ids_dict = dict()
    num_seats = -1

    # openstv options
    method_name = "Approval"
    # strong_tie_break_method = None # None means default
    # weak_tie_break_method = None # None means default
    # digits_precision = None # None means default

    # report object
    report = None

    def init(self):
        self.ballots_path = tempfile.mktemp(".blt")

        self.ballots = []
        self.answer_to_ids_dict = dict()

    def parse_vote(self, number, question):
        vote_str = str(number)
        tab_size = len(str(len(question['answers']) + 2))

        # fix add zeros
        if len(vote_str) % tab_size != 0:
            num_zeros = (tab_size - (len(vote_str) % tab_size)) % tab_size
            vote_str = "0" * num_zeros + vote_str

        ret = []
        for i in range(int(len(vote_str) / tab_size)):
            option = int(vote_str[i*tab_size: (i+1)*tab_size]) - 1
            if option < 0:
                # invalid vote
                raise Exception()
            if option < len(question['answers']):
                option_str = question['answers'][option]['value']
            if option >= len(question['answers']):
                # invalid/blank vote
                raise Exception()
            ret.append(option_str)

        # detect invalid vote
        if len(ret) < question['min'] or len(ret) > question['max'] or\
                len(set(ret)) != len(ret):
            raise Exception()

        return ret

    def pre_tally(self, result):
        '''
        Function called once before the tally begins
        '''
        import codecs
        import os
        if not os.path.exists(os.path.dirname(self.ballots_path)):
            os.makedirs(os.path.dirname(self.ballots_path))
        self.ballots_file = codecs.open(self.ballots_path, encoding='utf-8', mode='w')

        question = result[self.question_num]
        self.num_seats = question['num_seats']

        # fill answer to dict
        i = 1
        for answer in question['answers']:
            self.answer_to_ids_dict[answer['value']] = i
            i += 1

        # write the header of the BLT File
        # See format here: https://code.google.com/p/droop/wiki/BltFileFormat
        self.ballots_file.write('%d %d\n' % (len(question['answers']), question['num_seats']))

    def answer2id(self, answer):
        '''
        Converts the answer to an id.
        @return the id or -1 if not found
        '''
        return self.answer_to_ids_dict.get(answer, -1)

    def find_ballot(self, answers):
        '''
        Find a ballot with the same answers as the one given in self.ballots.
        Returns the ballot or None if not found.
        '''
        for ballot in self.ballots:
            if ballot['answers'] == answers:
                return ballot

        return None

    def add_vote(self, voter_answers, result, is_delegated):
        '''
        Add to the count a vote from a voter
        '''
        answers = [self.answer2id(a) for a in voter_answers[self.question_num]['choices']]
        # we got ourselves an invalid vote, don't count it
        if -1 in answers:
            return

        ballot = self.find_ballot(answers)
        # if ballot found, increment the count. Else, create a ballot and add it
        if ballot:
            ballot['votes'] += 1
        else:
            self.ballots.append(dict(votes=1, answers=answers))

    def finish_writing_ballots_file(self, result):
        # write the ballots
        question = result[self.question_num]
        for ballot in self.ballots:
            self.ballots_file.write('%d %s 0\n' % (ballot['votes'],
                ' '.join([str(a) for a in ballot['answers']])))
        self.ballots_file.write('0\n')

        # write the candidates
        for answer in question['answers']:
            name = answer['value']
            name.encode('utf-8')
            ans = u'"%s"\n' % name
            self.ballots_file.write(ans)

        q = '"%s"\n' % question['question'].replace("\n", "")
        q.encode('utf-8')
        self.ballots_file.write(q)
        self.ballots_file.close()

    def perform_tally(self):
        '''
        Actually calls to openstv to perform the tally
        '''
        from openstv.ballots import Ballots
        from openstv.plugins import getMethodPlugins

        # get voting and report methods
        methods = getMethodPlugins("byName", exclude0=False)

        # generate ballots
        dirtyBallots = Ballots()
        dirtyBallots.loadKnown(self.ballots_path, exclude0=False)
        dirtyBallots.numSeats = self.num_seats
        cleanBallots = dirtyBallots.getCleanBallots()

        # create and configure election
        e = methods[self.method_name](cleanBallots)

        # run election and generate the report
        # from celery.contrib import rdb; rdb.set_trace()
        e.runElection()

        # generate report
        from .json_report import JsonReport
        self.report = JsonReport(e)
        self.report.generateReport()

    def fill_results(self, result):

        json_report = self.report.json
        question = result[self.question_num]
        question['total_votes'] = json_report['ballots_count']
        question['dirty_votes'] = json_report['dirty_ballots_count'] - json_report['ballots_count']
        json_report['winners'] = [winner.decode('utf-8') for winner in json_report['winners']]
        question['winners'] = json_report['winners']

        # we cant use ballots_count as there is more than one vote per ballot
        total_votes = 0
        for name in json_report['answers']:
            total_votes += json_report['answers'][name]

        for answer in question['answers']:
            name = answer['value']
            name.encode('utf-8')

            answer['total_count'] = json_report['answers'][name]
            if total_votes > 0:
                answer['total_count_percentage'] = (answer['total_count'] * 100.0) / total_votes
            else:
                answer['total_count_percentage'] = 0

    def post_tally(self, result):
        '''
        Once all votes have been added, this function actually save them to
        disk and then calls openstv to perform the tally
        '''
        self.finish_writing_ballots_file(result)
        self.perform_tally()
        self.fill_results(result)

    def get_log(self):
        '''
        Returns the tally log. Called after post_tally()
        '''
        return self.report.json