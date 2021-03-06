import torch
import torch.nn as nn
import torchvision.models as models
from torch.nn.utils.rnn import pack_padded_sequence as pack
from torch.nn.utils.rnn import pad_packed_sequence as unpack
from torch.autograd import Variable
import torch.nn.functional as f

class EncoderCNN(nn.Module):
    def __init__(self, embed_size):
        """Load the pretrained VGG16 and replace top fc layer."""
        super(EncoderCNN, self).__init__()
        vgg16 = models.vgg16(pretrained=True)
        self.vgg16_feat = vgg16.features
        self.vgg16_clf = nn.Sequential(*list(vgg16.classifier.children())[:-1])
        self.vgg16_feat.eval()
        self.vgg16_clf.eval()
        self.linear = nn.Linear(4096, embed_size)
        self.bn = nn.BatchNorm1d(embed_size, momentum=0.01)
        self.init_weights()
        
    def init_weights(self):
        """Initialize the weights."""
        self.linear.weight.data.normal_(0.0, 0.02)
        self.linear.bias.data.fill_(0)
        
    def forward(self, images):
        """Extract the image feature vectors."""
        features = self.vgg16_feat(images)
        features = features.view(features.size(0), -1)
        features = self.vgg16_clf(features)
        features = self.bn(self.linear(features))
        return features


class LayoutEncoder(nn.Module):
    def __init__(self, layout_encoding_size, hidden_size, vocab_size, num_layers):
        """Set the hyper-parameters and build the layers."""
        super(LayoutEncoder, self).__init__()
        self.label_encoder = nn.Embedding(vocab_size, layout_encoding_size)
        self.location_encoder = nn.Linear(4, layout_encoding_size)
        self.lstm = nn.LSTM(layout_encoding_size, hidden_size, num_layers, batch_first=True)
        self.init_weights()

    def init_weights(self):
        """Initialize weights."""
        self.label_encoder.weight.data.uniform_(-0.1, 0.1)
        self.location_encoder.weight.data.uniform_(-0.1, 0.1)
        self.location_encoder.bias.data.fill_(0)

    def forward(self, label_seqs, location_seqs, lengths):
        # sort label sequences and location sequences in batch dimension according to length
        batch_idx = sorted(range(len(lengths)), key=lambda k: lengths[k], reverse=True)
        reverse_batch_idx = torch.LongTensor([batch_idx.index(i) for i in range(len(batch_idx))])

        lens_sorted = sorted(lengths, reverse=True)
        label_seqs_sorted = torch.index_select(label_seqs, 0, torch.LongTensor(batch_idx))
        location_seqs_sorted = torch.index_select(location_seqs, 0, torch.LongTensor(batch_idx))

        #assert torch.equal(torch.index_select(label_seqs_sorted, 0, reverse_batch_idx), label_seqs)
        #assert torch.equal(torch.index_select(location_seqs_sorted, 0, reverse_batch_idx), location_seqs)

        if torch.cuda.is_available():
            reverse_batch_idx = reverse_batch_idx.cuda()
            label_seqs_sorted = label_seqs_sorted.cuda()
            location_seqs_sorted = location_seqs_sorted.cuda()

        # create Variables
        label_seqs_sorted_var = Variable(label_seqs_sorted, requires_grad=False)
        location_seqs_sorted_var = Variable(location_seqs_sorted, requires_grad=False)

        # encode label sequences
        label_encoding = self.label_encoder(label_seqs_sorted_var)

        # encode location sequences
        location_seqs_sorted_var = location_seqs_sorted_var.view(-1, 4)
        location_encoding = self.location_encoder(location_seqs_sorted_var)
        location_encoding = location_encoding.view(label_encoding.size(0), -1, location_encoding.size(1))

        # layout encoding - batch_size x max_seq_len x embed_size
        layout_encoding = label_encoding + location_encoding
        packed = pack(layout_encoding, lens_sorted, batch_first=True)
        hiddens, _ = self.lstm(packed)

        # unpack hiddens and get last hidden vector
        hiddens_unpack = unpack(hiddens, batch_first=True)[0]  # batch_size x max_seq_len x embed_size
        last_hidden_idx = torch.zeros(hiddens_unpack.size(0), 1, hiddens_unpack.size(2)).long()
        for i in range(hiddens_unpack.size(0)):
            last_hidden_idx[i, 0, :] = lens_sorted[i] - 1
        if torch.cuda.is_available():
            last_hidden_idx = last_hidden_idx.cuda()
        last_hidden = torch.gather(hiddens_unpack, 1, Variable(last_hidden_idx, requires_grad=False))  # batch_size x 1 x embed_size
        last_hidden = torch.squeeze(last_hidden, 1)  # batch_size x embed_size

        # convert back to original batch order
        last_hidden = torch.index_select(last_hidden, 0, Variable(reverse_batch_idx, requires_grad=False))

        return last_hidden, hiddens_unpack
    
    
class DecoderRNN(nn.Module):
    def __init__(self, converter, embed_size, hidden_size, vocab_size, num_layers):
        """Set the hyper-parameters and build the layers."""
        super(DecoderRNN, self).__init__()
        self.embed = nn.Embedding(vocab_size, embed_size)
        self.lstm = nn.LSTM(embed_size, hidden_size, num_layers, batch_first=True)
        self.linear = nn.Linear(hidden_size, vocab_size)
        self.attn = nn.Linear(hidden_size, embed_size)
        self.softmax = nn.Softmax(dim=2)
        self.sigmoid = nn.Sigmoid()
        self.pgen_encoder = nn.Linear(embed_size, 1)
        self.pgen_decoder = nn.Linear(hidden_size, 1)
        self.converter = converter
        self.init_weights()
    
    def init_weights(self):
        """Initialize weights."""
        self.embed.weight.data.uniform_(-0.1, 0.1)
        for linear in [self.linear, self.attn, self.pgen_encoder, self.pgen_decoder]:
            linear.weight.data.uniform_(-0.1, 0.1)
            linear.bias.data.fill_(0)
        
    def forward(self, features, captions, lengths, encoder_input, encoder_output):
        """Decode image feature vectors and generates captions."""
        # Base LSTM
        embeddings = self.embed(captions)
        embeddings = torch.cat((features.unsqueeze(1), embeddings), 1)
        packed = pack(embeddings, lengths, batch_first=True)
        hiddens, _ = self.lstm(packed)

        # Creating Mask for Attention
        mask = encoder_input.eq(0).unsqueeze(1)
        mask = mask.expand(encoder_input.size()[0], captions.size()[1], encoder_input.size()[1]) # BxT_dxT_e

        if torch.cuda.is_available():
            mask = mask.cuda()
        # Calculating Attention Score
        unpacked_hiddens = unpack(hiddens, batch_first=True)[0]  # BxT_dxH
        encoder_output_permuted = encoder_output.permute(0,2,1) # BxHxT_e
        attn_weights_raw = torch.bmm(self.attn(unpacked_hiddens), encoder_output_permuted) #BxT_dxT_e
        attn_weights_raw.data.masked_fill_(mask, -float('inf')) 
        attn_weights = self.softmax(attn_weights_raw) 
        encoder_output = torch.bmm(attn_weights, encoder_output) # BxT_dxH

        # Pointer Generator
        p_gen = self.sigmoid(self.pgen_encoder(encoder_output) + self.pgen_decoder(unpacked_hiddens))

        one_hot = torch.FloatTensor(encoder_input.size()[0], encoder_input.size()[1], 100).zero_()
        one_hot.scatter_(2, encoder_input.unsqueeze(2), 1)
        one_hot = Variable(one_hot) # BxT_ex91
        
        if torch.cuda.is_available():
            one_hot = one_hot.cuda()
        
        one_hot_vocab = torch.mm(one_hot.view(-1, 100), self.converter).view(one_hot.size()[0], one_hot.size()[1], -1)

        outputs_regular = self.linear(unpacked_hiddens)
        attn_weights_raw = torch.clamp(attn_weights_raw,min=-1000,max=1000)
        outputs_pointer = torch.bmm(attn_weights_raw, one_hot_vocab)
        outputs = p_gen * outputs_regular + (1 - p_gen) * outputs_pointer
        
        outputs = pack(outputs, lengths, batch_first=True)[0] 
        return outputs
    
    def sample(self, features, encoder_input, encoder_output,states=None):
        """Samples captions for given image features (Greedy search)."""

        sampled_ids = []
        inputs = features.unsqueeze(1)

        # Encoder input
        one_hot = torch.FloatTensor(encoder_input.size()[0], encoder_input.size()[1], 100).zero_()
        one_hot.scatter_(2, encoder_input.unsqueeze(2), 1)
        one_hot = Variable(one_hot) # BxT_ex91

        if torch.cuda.is_available():
            one_hot = one_hot.cuda()
        one_hot_vocab = torch.mm(one_hot.view(-1, 100), self.converter).view(one_hot.size()[0], one_hot.size()[1], -1)
        
        for i in range(20):

            squeezed_hidden, states = self.lstm(inputs, states)       #Bx1xhid  

            # Creating Mask for Attention
            mask = encoder_input.eq(0).unsqueeze(1)
            mask = mask.expand(encoder_input.size()[0], 1, encoder_input.size()[1]) # Bx1xT_e
            if torch.cuda.is_available():
                mask = mask.cuda()

            # Calculating Attention Score
            encoder_output_permuted = encoder_output.permute(0,2,1) # BxHxT_e
            attn_weights_raw = torch.bmm(self.attn(squeezed_hidden), encoder_output_permuted) #Bx1xT_e
 
            attn_weights_raw.data.masked_fill_(mask, -float('inf')) 
            attn_weights = self.softmax(attn_weights_raw) 
            encoder_output_new = torch.bmm(attn_weights, encoder_output) # Bx1xH


            # Pointer Generator
            p_gen = self.sigmoid(self.pgen_encoder(encoder_output_new) + self.pgen_decoder(squeezed_hidden))

            outputs_regular = self.linear(squeezed_hidden)
            attn_weights_raw = torch.clamp(attn_weights_raw,min=-1000,max=1000)
            outputs_pointer = torch.bmm(attn_weights_raw, one_hot_vocab)
            outputs = p_gen * outputs_regular + (1 - p_gen) * outputs_pointer
            
            # Get the word with max probability
            outputs = outputs.squeeze(1)
            predicted = outputs.max(1)[1]
            inputs = self.embed(predicted).unsqueeze(1)
            predicted = predicted.unsqueeze(1)
            sampled_ids.append(predicted)

        sampled_ids = torch.cat(sampled_ids, 1)                  # (batch_size, 20)

        return sampled_ids
